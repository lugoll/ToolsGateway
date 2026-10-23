"""Shared plumbing of the web pages: the current session, CSRF, rendering, flash messages."""
from __future__ import annotations

import functools
import secrets
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from ..auth.ratelimit import LoginLimiter
from ..auth.sessions import Session, Sessions

TEMPLATES = Path(__file__).parent / "templates"
FLASH_KEEP = 500


class Web:
    """Everything a page handler needs; one instance per app (request.app.state.web)."""

    def __init__(self, services):
        self.s = services
        self.sessions = Sessions(services.db, services.users, services.settings.secure_cookies)
        self.limiter = LoginLimiter()
        self.public_origin = _origin(services.settings.public_url)
        self.env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]),
                               trim_blocks=True, lstrip_blocks=True)
        self._flashes: OrderedDict[int, list[tuple[str, str]]] = OrderedDict()

    # ------------------------------------------------------------- sessions
    def session(self, request: Request) -> Session | None:
        if "session" not in request.scope.setdefault("state", {}):
            value = request.cookies.get(self.sessions.cookie_name)
            request.scope["state"]["session"] = self.sessions.lookup(value)
        return request.scope["state"]["session"]

    def flash(self, session: Session, message: str, kind: str = "ok") -> None:
        self._flashes.setdefault(session.id, []).append((kind, message))
        while len(self._flashes) > FLASH_KEEP:
            self._flashes.popitem(last=False)

    # ------------------------------------------------------------ rendering
    def render(self, request: Request, template: str, status: int = 200, **context) -> HTMLResponse:
        session = self.session(request)
        flashes = self._flashes.pop(session.id, []) if session else []
        html = self.env.get_template(template).render(
            request=request, session=session, me=session.user if session else None,
            csrf=session.csrf if session else "", flashes=flashes,
            public_url=self.s.settings.public_url, **context)
        return HTMLResponse(html, status_code=status)

    @staticmethod
    def redirect(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def web(request: Request) -> Web:
    return request.app.state.web


async def form(request: Request) -> dict[str, str]:
    """The posted form, after the CSRF check (every POST of a logged-in page)."""
    data = await request.form()
    session = web(request).session(request)
    token = str(data.get("csrf", ""))
    if session is None or not secrets.compare_digest(token, session.csrf):
        raise HTTPException(403, "invalid form token: reload the page and try again")
    return {k: v for k, v in data.items() if isinstance(v, str)}


def page(admin: bool = False):
    """Decorator: the handler runs only for a logged-in (admin) user; receives (request, session)."""
    def wrap(handler):
        @functools.wraps(handler)
        async def inner(request: Request):
            session = web(request).session(request)
            if session is None:
                return Web.redirect("/login?next=" + request.url.path)
            if admin and not session.user.is_admin:
                raise HTTPException(403, "admins only")
            return await handler(request, session)
        return inner
    return wrap
