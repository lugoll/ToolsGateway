"""Login, logout and the user's own password."""
from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.routing import Route

from ...auth.passwords import PasswordPolicyError
from ..common import Web, form, page, web


def _safe_next(target: str | None) -> str:
    return target if target and target.startswith("/") and not target.startswith("//") else "/"


async def login(request: Request):
    w = web(request)
    if request.method == "GET":
        if w.session(request):
            return Web.redirect("/")
        return w.render(request, "login.html", next=_safe_next(request.query_params.get("next")))
    data = await request.form()                 # no session yet: WebSecurity's origin check guards it
    name = str(data.get("name", "")).strip().lower()
    password = str(data.get("password", ""))
    target = _safe_next(str(data.get("next", "")))
    address = request.client.host if request.client else "?"
    wait = w.limiter.wait(address, name)
    if wait > 0:
        return w.render(request, "login.html", status=429, next=target, name=name,
                        error=f"Too many failed attempts. Try again in {int(wait) + 1} s.")
    user = await asyncio.to_thread(w.s.users.authenticate, name, password)   # argon2 is slow on purpose
    if user is None:
        w.limiter.failed(address, name)
        w.s.audit.record(name or "?", "admin", "login_failed", ok=False, detail={"address": address})
        return w.render(request, "login.html", status=401, next=target, name=name,
                        error="Wrong name or password.")
    w.limiter.succeeded(address, name)
    old = request.cookies.get(w.sessions.cookie_name)
    w.sessions.end(old)                          # never reuse a session id across a login
    response = Web.redirect(target)
    response.set_cookie(value=w.sessions.create(user), **w.sessions.cookie_args())
    w.s.audit.record(user.name, "admin", "login", detail={"address": address})
    return response


async def logout(request: Request):
    w = web(request)
    await form(request)
    w.sessions.end(request.cookies.get(w.sessions.cookie_name))
    response = Web.redirect("/login")
    response.delete_cookie(w.sessions.cookie_name, path="/", secure=w.sessions.secure, httponly=True,
                           samesite="strict")
    return response


@page()
async def account(request: Request, session):
    w = web(request)
    if request.method == "GET":
        return w.render(request, "account.html")
    data = await form(request)
    if not w.s.users.authenticate(session.user.name, data.get("current", "")):
        return w.render(request, "account.html", status=400, error="The current password is wrong.")
    if data.get("new") != data.get("again"):
        return w.render(request, "account.html", status=400, error="The new passwords differ.")
    try:
        w.s.users.set_password(session.user.id, data.get("new", ""))
    except PasswordPolicyError as exc:
        return w.render(request, "account.html", status=400, error=str(exc))
    w.s.audit.record(session.user.name, "admin", "password_changed", detail={"by": session.user.name})
    response = Web.redirect("/login")             # every session ended with the old password
    response.delete_cookie(w.sessions.cookie_name, path="/")
    return response


routes = [
    Route("/login", login, methods=["GET", "POST"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/account", account, methods=["GET", "POST"]),
]
