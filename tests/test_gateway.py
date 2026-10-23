"""The gateway end to end over HTTP: users, tokens, routing, meta tools, remote proxy, native mail."""
import asyncio

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

import fake_remote
from gateway.app import create_app
from gateway.connectors.mail import tools as mail_tools
from test_mail_tools import FakeBackend


@pytest.fixture
def remote(serve):
    return serve(fake_remote.app()) + "/mcp"


@pytest.fixture
def world(services, remote, monkeypatch):
    """lukas: a remote 'tasks' server and two mailboxes; masha: nothing yet."""
    real = mail_tools.make_backend
    monkeypatch.setattr(mail_tools, "make_backend",
                        lambda acc, cfg, ctx: FakeBackend(acc) if acc.backend == "imap" else real(acc, cfg, ctx))
    s = services
    lukas = s.users.create("lukas", "lukas-password-1")
    masha = s.users.create("masha", "masha-password-1")
    cat = s.catalog
    s.connections.create(lukas.id, cat.get("fake-remote"), "tasks", "Tasks", {"url": remote},
                         {"token": "upstream-secret"}, by="lukas")
    s.connections.create(lukas.id, cat.get("icloud-mail"), "icloud", "iCloud", {"email": "l@icloud.com"},
                         {"password": "app-pw"}, by="lukas")
    s.connections.create(lukas.id, cat.get("gmail"), "gmail", "Gmail", {"email": "l@gmail.com"}, {}, by="lukas")
    lukas_token, _ = s.tokens.create(lukas.id, "hermes")
    masha_token, _ = s.tokens.create(masha.id, "laptop")
    return {"lukas": lukas, "masha": masha, "lukas_token": lukas_token, "masha_token": masha_token}


def auth(token):
    return {"Authorization": f"Bearer {token}"}


async def mcp_client_call(url, token, tool=None, args=None):
    async with Client(StreamableHttpTransport(url, headers=auth(token))) as c:
        names = sorted(t.name for t in await c.list_tools())
        result = await c.call_tool(tool, args or {}, raise_on_error=False) if tool else None
    return names, result


def test_tenant_isolation_and_auth(services, world, serve):
    base = serve(create_app(services))
    lk, mt = world["lukas_token"], world["masha_token"]
    assert httpx.get(f"{base}/health/live").status_code == 200
    assert httpx.get(f"{base}/api/v1/lukas/tools").status_code == 401
    assert httpx.get(f"{base}/api/v1/lukas/tools", headers=auth("tgw_wrong")).status_code == 401
    assert httpx.get(f"{base}/api/v1/lukas/tools", headers=auth(mt)).status_code == 403      # masha's token
    assert httpx.get(f"{base}/api/v1/nobody/tools", headers=auth(mt)).status_code == 403     # same answer
    assert httpx.post(f"{base}/mcp/lukas/mail", headers=auth(mt), json={}).status_code == 403
    assert httpx.get(f"{base}/api/v1/lukas/tools", headers=auth(lk)).status_code == 200
    assert httpx.get(f"{base}/api/v1/masha/tools", headers=auth(mt)).json() == {"tools": [], "errors": {}}
    # A revoked token stops working at once.
    token, row = services.tokens.create(world["lukas"].id, "temp")
    assert httpx.get(f"{base}/api/v1/lukas/status", headers=auth(token)).status_code == 200
    services.tokens.revoke(world["lukas"].id, row.id)
    assert httpx.get(f"{base}/api/v1/lukas/status", headers=auth(token)).status_code == 401


def test_integration_endpoints_and_filters(services, world, serve):
    base = serve(create_app(services))
    lk = world["lukas_token"]
    names, result = asyncio.run(mcp_client_call(f"{base}/mcp/lukas/tasks", lk, "hello", {"name": "x"}))
    assert names == ["hello"]                      # delete_* off by default, send_* forbidden
    text = result.content[0].text
    assert "hi x" in text and "auth=Bearer upstream-secret" in text   # header from the encrypted secret
    assert lk not in text                                             # the gateway token never goes upstream
    names, _ = asyncio.run(mcp_client_call(f"{base}/mcp/lukas/mail", lk))
    assert names == sorted(mail_tools.TOOLS)
    r = httpx.post(f"{base}/mcp/lukas/calendar", headers=auth(lk), json={})
    assert r.status_code == 404 and "connected: mail, tasks" in r.json()["error"]

    # The user enables delete_task, and tries to enable the forbidden tool: only the first works.
    c = next(c for c in services.connections.for_user(world["lukas"].id) if c.name == "tasks")
    services.connections.update(c, tools={"enabled": ["delete_task", "send_message"], "disabled": ["hello"]})
    names, _ = asyncio.run(mcp_client_call(f"{base}/mcp/lukas/tasks", lk))
    assert names == ["delete_task"]


def test_meta_tools(services, world, serve):
    base = serve(create_app(services))
    lk = world["lukas_token"]
    names, result = asyncio.run(mcp_client_call(f"{base}/mcp/lukas", lk, "list_integrations"))
    assert names == ["call_tool", "describe_tool", "list_integrations", "search_tools"]
    listed = {i["integration"]: i for i in result.structured_content["integrations"]}
    assert listed["mail"]["accounts"] == ["gmail", "icloud"] and listed["tasks"]["status"] == "ok"

    _, result = asyncio.run(mcp_client_call(f"{base}/mcp/lukas", lk, "search_tools", {"query": "draft mail"}))
    hits = result.structured_content["tools"]
    assert hits[0] == {"integration": "mail", "tool": "save_draft", "description": hits[0]["description"]}

    _, result = asyncio.run(mcp_client_call(f"{base}/mcp/lukas", lk, "describe_tool",
                                            {"integration": "tasks", "tool": "hello"}))
    assert result.structured_content["input_schema"]["required"] == ["name"]

    _, result = asyncio.run(mcp_client_call(f"{base}/mcp/lukas", lk, "call_tool",
                                            {"integration": "tasks", "tool": "hello", "arguments": {"name": "m"}}))
    assert "hi m" in result.content[0].text
    _, result = asyncio.run(mcp_client_call(f"{base}/mcp/lukas", lk, "call_tool",
                                            {"integration": "tasks", "tool": "send_message",
                                             "arguments": {"to": "x"}}))
    assert result.is_error and "not found" in result.content[0].text
    _, result = asyncio.run(mcp_client_call(f"{base}/mcp/lukas", lk, "call_tool",
                                            {"integration": "mail", "tool": "list_accounts"}))
    accounts = {a["account"]: a for a in result.structured_content["accounts"]}
    assert "needs authorisation" in accounts["gmail"]["status"]       # no Google token yet


def test_rest_status_and_audit(services, world, serve):
    base = serve(create_app(services))
    lk = world["lukas_token"]
    r = httpx.post(f"{base}/api/v1/lukas/tool-invocations", headers=auth(lk),
                   json={"tool": "mail/move_email", "args": {"account": "icloud", "id": "INBOX|1|7", "to": "Trash"}})
    assert r.status_code == 200 and r.json()["result"]["structuredContent"]["moved"]
    assert httpx.post(f"{base}/api/v1/lukas/tool-invocations", headers=auth(lk), json={"x": 1}).status_code == 400
    r = httpx.post(f"{base}/api/v1/lukas/tool-invocations", headers=auth(lk),
                   json={"tool": "tasks/hello", "args": {"name": "secret-value"}})
    assert r.status_code == 200
    status = httpx.get(f"{base}/api/v1/lukas/status", headers=auth(lk)).json()
    assert status["ready"] and status["integrations"]["tasks"]["tools"] == 1

    entries = services.audit.query(user="lukas")
    change = next(e for e in entries if e.kind == "change")
    assert change.action == "move" and change.connection == "icloud" and change.detail["destination_kind"] == "trash"
    call = next(e for e in entries if e.kind == "call" and e.action == "hello")
    assert call.integration == "tasks" and call.detail["args"] == ["name"]
    assert "secret-value" not in str([e.detail for e in entries])
    assert services.audit.query(user="masha") == []


async def test_rebuild_on_change_and_broken_connection_isolated(services, world, remote):
    s = services
    lukas = world["lukas"]
    rt = await s.registry.get(lukas)
    assert set(rt.integrations) == {"mail", "tasks"}
    assert await s.registry.get(lukas) is rt                      # cached
    s.connections.create(lukas.id, s.catalog.get("remote-mcp"), "dead", "Dead", {"url": "http://127.0.0.1:9/mcp"},
                         {}, by="lukas")
    rt2 = await s.registry.get(lukas)
    assert rt2 is not rt and set(rt2.integrations) == {"dead", "mail", "tasks"}
    status = await rt2.status()
    assert status["ready"] is False and status["integrations"]["dead"]["status"] == "error"
    assert status["integrations"]["tasks"]["status"] == "ok"      # the others keep serving
    tasks = next(c for c in s.connections.for_user(lukas.id) if c.name == "tasks")
    s.connections.update(tasks, enabled=False)
    rt3 = await s.registry.get(lukas)
    assert "tasks" not in rt3.integrations
    await s.registry.close()


async def test_remote_tool_list_is_cached_per_runtime(services, world):
    s = services
    lukas = world["lukas"]
    rt = await s.registry.get(lukas)
    upstream = rt.integrations["tasks"].server
    fetches = []
    real = upstream.list_tools

    async def counting(*args, **kwargs):
        fetches.append(1)
        return await real(*args, **kwargs)
    upstream.list_tools = counting
    tools, _ = await rt.tool_index("tasks")
    first = len(fetches)
    assert [t["name"] for t in tools] == ["hello"] and first
    for _ in range(3):                                            # later meta calls: no round trip
        await rt.tool_index("tasks")
    assert len(fetches) == first
    await rt.status(fresh=True)                                   # "fresh" asks upstream again
    assert len(fetches) > first
    tasks = next(c for c in s.connections.for_user(lukas.id) if c.name == "tasks")
    s.connections.update(tasks, tools={"enabled": ["delete_task"]})
    tools, _ = await (await s.registry.get(lukas)).tool_index("tasks")   # a toggle never waits for the TTL
    assert sorted(t["name"] for t in tools) == ["delete_task", "hello"]
    await s.registry.close()


def test_remote_name_cannot_shadow_native_integration(services, world):
    from gateway.storage.connections import ConnectionSetupError
    with pytest.raises(ConnectionSetupError, match="built-in integration"):
        services.connections.create(world["lukas"].id, services.catalog.get("remote-mcp"), "mail", "x",
                                    {"url": "https://x.org/mcp"}, {}, by="lukas")
