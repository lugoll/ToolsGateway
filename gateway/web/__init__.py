"""The web UI: server-rendered pages (Jinja2), plain HTML forms, no JavaScript.

Logged-in users manage their connections, secrets and API tokens and read
their audit log; admins also manage users and instance settings.
Security: web/security.py (headers, cross-site check), common.form (CSRF
token), auth/sessions.py (cookies), auth/ratelimit.py (login throttling).
"""
from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from .common import Web
from .routes import routes as page_routes
from .security import WebSecurity

STATIC = Path(__file__).parent / "static"


def create_web(services, routes, lifespan) -> WebSecurity:
    w = Web(services)

    async def http_error(request: Request, exc: HTTPException):
        return w.render(request, "error.html", status=exc.status_code, code=exc.status_code,
                        detail=exc.detail or "")

    app = Starlette(routes=[*routes, *page_routes, Mount("/static", StaticFiles(directory=STATIC), name="static")],
                    lifespan=lifespan, exception_handlers={HTTPException: http_error})
    app.state.web = w
    return WebSecurity(app, w.public_origin)
