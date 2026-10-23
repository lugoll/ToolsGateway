"""Connecting accounts with OAuth: Google (paste-back) and remote MCP servers (redirect + callback)."""
from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.routing import Route

from ...outbound.oauth.google import GoogleAuthError
from ...outbound.oauth.mcp import McpAuthError
from ...outbound.remote_mcp import render_headers
from ..common import form, page, web


def _owned(request: Request, session, provider: str):
    w = web(request)
    try:
        c = w.s.connections.get(int(request.path_params["cid"]), user_id=session.user.id)
    except ValueError:
        c = None
    entry = w.s.catalog.get(c.catalog_id) if c else None
    if c is None or entry is None or entry.oauth is None or entry.oauth.provider != provider:
        raise HTTPException(404, "no such connection")
    return c, entry


@page()
async def google_start(request: Request, session):
    w = web(request)
    c, entry = _owned(request, session, "google")
    try:
        url = w.s.google_oauth.start(session.user.id, c.id, c.settings.get("email", ""), entry.oauth.scopes)
    except GoogleAuthError as exc:
        w.flash(session, str(exc), "error")
        return w.redirect(f"/connections/{c.id}")
    return w.render(request, "oauth_google.html", c=c, url=url)


@page()
async def google_finish(request: Request, session):
    w = web(request)
    data = await form(request)
    try:
        pending, token = w.s.google_oauth.finish(session.user.id, data.get("pasted", ""))
    except GoogleAuthError as exc:
        w.flash(session, str(exc), "error")
        return w.redirect("/")
    c = w.s.connections.get(pending.connection_id, user_id=session.user.id)
    if c is None:
        raise HTTPException(404, "the connection is gone")
    w.s.connections.set_secret(c, "oauth_token", token, by=session.user.name)
    w.s.audit.record(session.user.name, "admin", "oauth_connected", integration=c.integration, connection=c.name,
                     detail={"provider": "google", "scopes": pending.scopes})
    ok, message = await w.s.registry.test_connection(session.user, c)
    w.flash(session, f"Connected. {'Works: ' if ok else 'But the test failed: '}{message}", "ok" if ok else "error")
    return w.redirect(f"/connections/{c.id}")


@page()
async def mcp_start(request: Request, session):
    w = web(request)
    c, entry = _owned(request, session, "mcp")
    values = {**entry.settings, **c.settings}
    url = entry.url or values.get("url")
    headers = render_headers(entry, values, w.s.secrets.all(c.id))
    try:
        auth_url = await w.s.mcp_oauth.start(session.user.id, c.id, url, headers, entry.oauth.scopes)
    except McpAuthError as exc:
        w.flash(session, str(exc), "error")
        return w.redirect(f"/connections/{c.id}")
    if auth_url is None:
        w.flash(session, "Already connected; nothing to do.")
        return w.redirect(f"/connections/{c.id}")
    return w.redirect(auth_url)


async def mcp_callback(request: Request):
    """No session here (see outbound/oauth/mcp.py): the state identifies the flow."""
    w = web(request)
    try:
        user_id, cid, n = await w.s.mcp_oauth.finish(dict(request.query_params))
    except McpAuthError as exc:
        return w.render(request, "oauth_done.html", status=400, ok=False, message=str(exc), cid=None)
    user = w.s.users.get(user_id)
    c = w.s.connections.get(cid, user_id=user_id)
    if user and c:
        w.s.registry.invalidate(user.id)            # tokens were stored by the provider directly
        w.s.audit.record(user.name, "admin", "oauth_connected", integration=c.integration, connection=c.name,
                         detail={"provider": "mcp"})
    return w.render(request, "oauth_done.html", ok=True, message=f"Connected, {n} tools available.", cid=cid)


routes = [
    Route("/oauth/google/start/{cid}", google_start),
    Route("/oauth/google/finish", google_finish, methods=["POST"]),
    Route("/oauth/mcp/start/{cid}", mcp_start),
    Route("/oauth/mcp/callback", mcp_callback),
]
