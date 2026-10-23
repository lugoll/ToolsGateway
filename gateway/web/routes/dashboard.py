"""The start page: the user's endpoints, integrations and connections; tokens; the audit log."""
from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.routing import Route

from ..common import form, page, web


@page()
async def dashboard(request: Request, session):
    w = web(request)
    user = session.user
    runtime = await w.s.registry.get(user)
    status = await runtime.status()
    connections = w.s.connections.for_user(user.id)
    base = f"{w.s.settings.public_url}/mcp/{user.name}"
    return w.render(request, "dashboard.html", base=base, integrations=status["integrations"],
                    connections=connections, catalog=w.s.catalog)


@page()
async def tokens(request: Request, session):
    w = web(request)
    user = session.user
    created = None
    if request.method == "POST":
        data = await form(request)
        if data.get("action") == "create":
            created, row = w.s.tokens.create(user.id, data.get("label", ""))
            w.s.audit.record(user.name, "admin", "token_created", detail={"label": row.label, "prefix": row.prefix})
        elif data.get("action") == "revoke":
            try:
                token_id = int(data.get("id", ""))
            except ValueError:
                raise HTTPException(400, "bad token id") from None
            if w.s.tokens.revoke(user.id, token_id):
                w.s.audit.record(user.name, "admin", "token_revoked", detail={"id": token_id})
                w.flash(session, "Token revoked.")
            return w.redirect("/tokens")
    return w.render(request, "tokens.html", tokens=w.s.tokens.for_user(user.id), created=created,
                    base=f"{w.s.settings.public_url}/mcp/{user.name}")


@page()
async def audit(request: Request, session):
    w = web(request)
    me = session.user
    who = request.query_params.get("user") if me.is_admin else me.name
    try:
        before = int(request.query_params.get("before") or 0) or None
    except ValueError:
        before = None
    entries = w.s.audit.query(user=who or None, kind=request.query_params.get("kind") or None,
                              integration=request.query_params.get("integration") or None,
                              before=before, limit=100)
    return w.render(request, "audit.html", entries=entries, who=who or "",
                    kind=request.query_params.get("kind") or "",
                    integration=request.query_params.get("integration") or "",
                    users=[u.name for u in w.s.users.all()] if me.is_admin else [],
                    next_before=entries[-1].id if len(entries) == 100 else None)


routes = [
    Route("/", dashboard),
    Route("/tokens", tokens, methods=["GET", "POST"]),
    Route("/audit", audit),
]
