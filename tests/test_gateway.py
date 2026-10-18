"""Gateway core against a real stdio backend (tests/fake_backend.py) over HTTP."""
import json

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from scotty_gateway.app import Gateway
from scotty_gateway.config import load
from conftest import TOKEN, fake_stdio, write_config

AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def gateway(env):
    cfg = load(write_config(env, {
        "fake-tester": fake_stdio(env, deny=["send_*"]),
        "spare-tester": fake_stdio(env, namespace="spare", allow=["hello"]),
        "off-tester": {"domain": "tasks", "type": "http", "url": "http://nowhere", "disabled": True,
                       "note": "not today"},
    }))
    return Gateway(cfg)


def spawns(env) -> list[str]:
    log = env / "spawn.log"
    return log.read_text().split() if log.exists() else []


async def test_filters_namespace_and_disabled(gateway):
    tools, errors = await gateway.tool_index()
    names = {(t["server"], t["name"]) for t in tools}
    assert names == {("fake-tester", "hello"), ("fake-tester", "crash"), ("spare-tester", "spare_hello")}
    assert errors == {"off-tester": "disabled: not today"}
    assert await gateway.collisions() == []


async def test_invoke_shape_env_isolation_and_denied_tool(gateway):
    status, body = await gateway.invoke("fake-tester", "hello", {"name": "x"})
    assert status == 200 and body["result"]["isError"] is False
    text = body["result"]["content"][0]["text"]
    assert "secret=s3cret" in text and "leaked=None" in text
    status, body = await gateway.invoke("fake-tester", "send_email", {"to": "a"})
    assert status == 404 and "tool not found" in body["error"]
    status, body = await gateway.invoke("off-tester", "x", {})
    assert status == 409 and body["error"].startswith("not connected")
    status, body = await gateway.invoke("nope", "x", {})
    assert status == 404


async def test_backend_crash_recovers_and_process_is_reused(gateway, env):
    for _ in range(3):
        assert (await gateway.invoke("fake-tester", "hello", {"name": "a"}))[0] == 200
    before = len(spawns(env))
    status, body = await gateway.invoke("fake-tester", "crash", {})
    assert status == 200 and body["result"]["isError"] is True
    status, _ = await gateway.invoke("fake-tester", "hello", {"name": "after"})
    assert status == 200
    assert len(spawns(env)) == before + 1        # exactly one respawn


def test_http_auth_rest_and_mcp(gateway, env, serve):
    base = serve(gateway.asgi())
    assert httpx.get(f"{base}/health/live").status_code == 200
    assert httpx.get(f"{base}/api/v1/tools").status_code == 401
    assert httpx.get(f"{base}/api/v1/tools", headers={"Authorization": "Bearer wrong"}).status_code == 401
    listed = httpx.get(f"{base}/api/v1/tools", headers=AUTH).json()
    assert {t["name"] for t in listed["tools"]} == {"hello", "crash", "spare_hello"}
    r = httpx.post(f"{base}/api/v1/tool-invocations", headers=AUTH,
                   json={"tool": "fake-tester/hello", "args": {"name": "rest"}})
    assert r.status_code == 200 and "hi rest" in r.json()["result"]["content"][0]["text"]
    assert httpx.post(f"{base}/api/v1/tool-invocations", headers=AUTH, json={"x": 1}).status_code == 400
    assert httpx.get(f"{base}/mcp/unknown", headers=AUTH).status_code == 404
    ready = httpx.get(f"{base}/health/ready", headers=AUTH).json()
    assert ready["ready"] and ready["servers"]["fake-tester"]["tools"] == 2
    assert ready["servers"]["off-tester"]["status"] == "off"

    async def mcp_roundtrip():
        transport = StreamableHttpTransport(f"{base}/mcp/tasks", headers=AUTH)
        async with Client(transport) as c:
            names = {t.name for t in await c.list_tools()}
            result = await c.call_tool("spare_hello", {"name": "mcp"})
        return names, result.content[0].text

    import asyncio
    names, text = asyncio.run(mcp_roundtrip())
    assert names == {"hello", "crash", "spare_hello"} and "hi mcp" in text

    async def unauthorised():
        async with Client(StreamableHttpTransport(f"{base}/mcp/tasks")) as c:
            await c.list_tools()
    with pytest.raises(Exception):
        asyncio.run(unauthorised())


def test_audit_log(gateway, env):
    import asyncio
    from scotty_gateway import audit
    audit.setup(gateway.cfg.data_dir)
    asyncio.run(gateway.invoke("fake-tester", "hello", {"name": "secret-value"}))
    lines = (gateway.cfg.data_dir / "audit.jsonl").read_text().splitlines()
    entry = json.loads(lines[-1])
    assert entry["server"] == "fake-tester" and entry["tool"] == "hello" and entry["ok"]
    assert entry["args"] == ["name"] and "secret-value" not in lines[-1]


async def test_dead_backend_is_an_error_and_others_keep_serving(env):
    import sys
    from fastmcp import Client
    cfg = load(write_config(env, {
        "fake-tester": fake_stdio(env),
        "dead-tester": {"domain": "tasks", "type": "stdio", "command": sys.executable,
                        "args": ["-c", "import sys; sys.exit(1)"], "namespace": "dead"},
    }))
    gw = Gateway(cfg)
    ready = await gw.ready()
    assert ready["ready"] is False
    assert ready["servers"]["dead-tester"]["status"] == "error"
    assert ready["servers"]["fake-tester"] == {"domain": "tasks", "status": "ok", "tools": 3}
    async with Client(gw.domains["tasks"]) as c:            # the domain endpoint still serves fake-tester
        assert {t.name for t in await c.list_tools()} == {"hello", "send_email", "crash"}
