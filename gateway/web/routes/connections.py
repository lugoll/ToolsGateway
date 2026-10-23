"""Connections: add one from the catalog, change settings, set secrets, toggle tools, test, delete.

The owner may do everything with a connection. An admin may open other users'
connections to overwrite a secret (a forgotten app password); nothing else, and
every such overwrite is in the owner's audit log.
"""
from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.routing import Route

from ...connectors import load_connector
from ...storage.connections import Connection, ConnectionSetupError
from ..common import form, page, web


def _default_name(entry) -> str:
    return entry.id.split("-")[0] if entry.kind == "native" else entry.integration


def _load(request: Request, session) -> tuple[Connection, bool]:
    """The connection in the path, and whether the session user owns it."""
    w = web(request)
    try:
        cid = int(request.path_params["cid"])
    except ValueError:
        raise HTTPException(404) from None
    c = w.s.connections.get(cid)
    if c is None or (c.user_id != session.user.id and not session.user.is_admin):
        raise HTTPException(404, "no such connection")
    return c, c.user_id == session.user.id


@page()
async def new(request: Request, session):
    w = web(request)
    groups: dict[str, list] = {}
    for entry in w.s.catalog:
        groups.setdefault("Other MCP servers" if entry.kind == "remote" else entry.integration.title(),
                          []).append(entry)
    return w.render(request, "catalog.html", groups=groups)


@page()
async def create(request: Request, session):
    w = web(request)
    entry = w.s.catalog.get(request.path_params["entry"])
    if entry is None:
        raise HTTPException(404, "no such catalog entry")
    values: dict = {"name": _default_name(entry), "label": entry.label}
    if request.method == "POST":
        data = await form(request)
        values = data
        secrets = {f.name: data.get(f.name, "") for f in entry.secret_fields}
        fields = {f.name: data.get(f.name, "" if f.type != "bool" else "off") for f in entry.fields if not f.secret}
        try:
            c = w.s.connections.create(session.user.id, entry, data.get("name", ""), data.get("label", ""),
                                       fields, secrets, by=session.user.name)
        except ConnectionSetupError as exc:
            return w.render(request, "connection_new.html", status=400, entry=entry, values=values, error=str(exc))
        w.s.audit.record(session.user.name, "admin", "connection_created", integration=c.integration,
                         connection=c.name, detail={"catalog": entry.id})
        w.flash(session, f"{c.label} added.")
        return w.redirect(f"/connections/{c.id}")
    return w.render(request, "connection_new.html", entry=entry, values=values)


@page()
async def detail(request: Request, session):
    w = web(request)
    c, owner = _load(request, session)
    entry = w.s.catalog.get(c.catalog_id)
    owner_user = w.s.users.get(c.user_id)
    tools, tools_error = [], None
    if owner and entry and entry.kind == "remote" and c.enabled:
        tools, tools_error = await w.s.registry.remote_tools(owner_user, c)
    capabilities = []
    if entry and entry.kind == "native":
        effective = w.s.connections.effective_capabilities(c)
        capabilities = [(cap, cap in effective) for cap in load_connector(c.integration).CAPABILITIES]
    return w.render(request, "connection.html", c=c, entry=entry, owner=owner, owner_user=owner_user,
                    secrets=w.s.secrets.describe(c.id), tools=tools, tools_error=tools_error,
                    capabilities=capabilities, endpoint=f"{w.s.settings.public_url}/mcp/{owner_user.name}/"
                                                       f"{c.integration}")


@page()
async def action(request: Request, session):
    w = web(request)
    c, owner = _load(request, session)
    data = await form(request)
    what = request.path_params["action"]
    entry = w.s.catalog.get(c.catalog_id)
    back = w.redirect(f"/connections/{c.id}")
    owner_user = w.s.users.get(c.user_id)

    def audit(action_name: str, **detail):
        w.s.audit.record(owner_user.name, "admin", action_name, integration=c.integration, connection=c.name,
                         detail={"by": session.user.name, **detail})

    if what == "secret":
        name = data.get("name", "")
        allowed = {f.name for f in entry.secret_fields} if entry else set()
        if name not in allowed:
            raise HTTPException(400, "unknown secret")
        if data.get("clear") == "1" and owner:
            w.s.connections.delete_secret(c, name)
            audit("secret_cleared", secret=name)
            w.flash(session, "Removed.")
        elif data.get("value"):
            w.s.connections.set_secret(c, name, data["value"], by=session.user.name)
            audit("secret_set" if owner else "secret_overwritten_by_admin", secret=name)
            w.flash(session, "Saved. The value cannot be shown again.")
        return back
    if not owner:
        raise HTTPException(403, "only the owner can change this connection")
    if what == "settings":
        fields = {f.name: data.get(f.name, "" if f.type != "bool" else "off")
                  for f in (entry.fields if entry else []) if not f.secret}
        try:
            w.s.connections.update(c, label=data.get("label") or c.label, values=fields)
        except ConnectionSetupError as exc:
            w.flash(session, str(exc), "error")
            return back
        audit("connection_changed")
        w.flash(session, "Saved.")
    elif what == "capabilities":
        known = load_connector(c.integration).CAPABILITIES
        chosen = [cap for cap in known if data.get(f"cap_{cap}") == "on"]
        w.s.connections.update(c, capabilities=chosen)
        audit("capabilities_changed", capabilities=chosen)
        w.flash(session, "Saved.")
    elif what == "tools":
        from ...mcp.filters import ToolFilter
        policy = ToolFilter(entry.tools.forbid, entry.tools.deny)          # catalog defaults only
        enabled, disabled = set(c.enabled_tools), set(c.disabled_tools)
        for name in data.get("listed", "").split(","):
            if not name or policy.state(name) == "forbidden":
                continue
            default_on = policy.state(name) == "on"
            on = data.get(f"tool_{name}") == "on"
            enabled.discard(name)
            disabled.discard(name)
            if on and not default_on:
                enabled.add(name)
            elif not on and default_on:
                disabled.add(name)
        w.s.connections.update(c, tools={"enabled": sorted(enabled), "disabled": sorted(disabled)})
        audit("tools_changed", enabled=sorted(enabled), disabled=sorted(disabled))
        w.flash(session, "Saved.")
    elif what == "enable":
        c = w.s.connections.update(c, enabled=not c.enabled)
        audit("connection_enabled" if c.enabled else "connection_disabled")
        w.flash(session, "Enabled." if c.enabled else "Disabled: the agent no longer sees it.")
    elif what == "test":
        ok, message = await w.s.registry.test_connection(owner_user, c)
        w.flash(session, ("Works: " if ok else "Failed: ") + message, "ok" if ok else "error")
    elif what == "delete":
        if data.get("confirm") != c.name:
            w.flash(session, f"Type '{c.name}' to confirm.", "error")
            return back
        w.s.connections.delete(c)
        audit("connection_deleted")
        w.flash(session, f"{c.label} deleted, with its secrets.")
        return w.redirect("/")
    else:
        raise HTTPException(404)
    return back


routes = [
    Route("/connections/new", new),
    Route("/connections/new/{entry}", create, methods=["GET", "POST"]),
    Route("/connections/{cid}", detail),
    Route("/connections/{cid}/{action}", action, methods=["POST"]),
]
