"""Admin pages: users (create, role, disable, password reset, delete) and instance settings."""
from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.routing import Route

from ...auth.passwords import PasswordPolicyError
from ...auth.users import UserError
from ..common import form, page, web

# Instance-wide secrets the admin sets: the Google OAuth client used by Gmail and Google Calendar.
INSTANCE_SECRETS = {
    "google_client_id": "Google OAuth client ID",
    "google_client_secret": "Google OAuth client secret",
}


@page(admin=True)
async def users(request: Request, session):
    w = web(request)
    error = None
    if request.method == "POST":
        data = await form(request)
        try:
            user = w.s.users.create(data.get("name", "").strip().lower(), data.get("password", ""),
                                    role="admin" if data.get("admin") == "on" else "user",
                                    display_name=data.get("display_name", ""))
        except (UserError, PasswordPolicyError) as exc:
            error = str(exc)
        else:
            w.s.audit.record(user.name, "admin", "user_created", detail={"by": session.user.name, "role": user.role})
            w.flash(session, f"User {user.name} created. Give them the password; they can change it under Account.")
            return w.redirect(f"/admin/users/{user.name}")
    return w.render(request, "admin_users.html", status=400 if error else 200, users=w.s.users.all(), error=error)


@page(admin=True)
async def user(request: Request, session):
    w = web(request)
    target = w.s.users.by_name(request.path_params["name"])
    if target is None:
        raise HTTPException(404, "no such user")
    if request.method == "POST":
        data = await form(request)
        what = data.get("action")
        try:
            if what == "update":
                w.s.users.update(target.id, display_name=data.get("display_name", ""),
                                 role="admin" if data.get("admin") == "on" else "user",
                                 disabled=data.get("disabled") == "on")
                w.s.audit.record(target.name, "admin", "user_changed", detail={"by": session.user.name})
                w.flash(session, "Saved.")
            elif what == "password":
                w.s.users.set_password(target.id, data.get("password", ""))
                w.s.audit.record(target.name, "admin", "password_reset", detail={"by": session.user.name})
                w.flash(session, f"Password of {target.name} set; their sessions are closed.")
            elif what == "delete":
                if data.get("confirm") != target.name:
                    raise UserError(f"type '{target.name}' to confirm")
                w.s.users.delete(target.id)
                w.s.audit.record(target.name, "admin", "user_deleted", detail={"by": session.user.name})
                w.flash(session, f"User {target.name} deleted with all connections, secrets and tokens.")
                return w.redirect("/admin/users")
        except (UserError, PasswordPolicyError) as exc:
            w.flash(session, str(exc), "error")
        if target.id == session.user.id and what in ("update", "password"):
            return w.redirect("/" if what == "update" else "/login")
        return w.redirect(f"/admin/users/{target.name}")
    return w.render(request, "admin_user.html", target=target, connections=w.s.connections.for_user(target.id),
                    tokens=w.s.tokens.for_user(target.id))


@page(admin=True)
async def settings(request: Request, session):
    w = web(request)
    if request.method == "POST":
        data = await form(request)
        name = data.get("name", "")
        if name not in INSTANCE_SECRETS:
            raise HTTPException(400, "unknown setting")
        if data.get("clear") == "1":
            w.s.secrets.delete_instance(name)
        elif data.get("value"):
            w.s.secrets.set_instance(name, data["value"].strip(), by=session.user.name)
        w.s.audit.record(session.user.name, "admin", "instance_setting_changed", detail={"setting": name})
        w.flash(session, "Saved.")
        return w.redirect("/admin/settings")
    return w.render(request, "admin_settings.html", names=INSTANCE_SECRETS, stored=w.s.secrets.describe_instance(),
                    redirect_uri=f"{w.s.settings.public_url}/oauth/mcp/callback")


routes = [
    Route("/admin/users", users, methods=["GET", "POST"]),
    Route("/admin/users/{name}", user, methods=["GET", "POST"]),
    Route("/admin/settings", settings, methods=["GET", "POST"]),
]
