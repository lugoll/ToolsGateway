"""The ASGI application: per-domain MCP endpoints, REST, health, bearer auth.

    /mcp/<domain>               stateless streamable HTTP, one FastMCP server per domain
    /api/v1/tools[?domain=]     tool list (debugging, CLI)
    /api/v1/tool-invocations    {"tool": "<server>/<tool>", "args": {}} without an LLM
    /health/live                the process is up (no auth; Docker healthcheck)
    /health/ready               per server: real tool listing, cached 30 s

Stateless on purpose: there are no MCP session ids to lose, so a restart of the
gateway (or of a backend behind it) never leaves a client holding a dead
session, which is the failure that made 1MCP drop all mail tools.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import time
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import GatewayConfig
from .servers import Backend, build_backend

log = logging.getLogger(__name__)

READY_TTL = 30.0
LIST_TIMEOUT = 45.0


class Gateway:
    def __init__(self, cfg: GatewayConfig):
        self.cfg = cfg
        self.backends: dict[str, Backend] = {n: build_backend(s, cfg) for n, s in cfg.servers.items()}
        self.domains: dict[str, FastMCP] = {}
        for domain in cfg.domains():
            dom = FastMCP(f"scotty-{cfg.member}-{domain}")
            for b in self.backends.values():
                if b.cfg.domain == domain and b.server is not None:
                    dom.mount(b.server, namespace=b.cfg.namespace)
            self.domains[domain] = dom
        self._ready: tuple[float, dict] | None = None
        self._ready_lock = asyncio.Lock()

    # ---------------------------------------------------------------- tools
    async def list_server_tools(self, b: Backend) -> list:
        return list(await asyncio.wait_for(b.server.list_tools(), LIST_TIMEOUT))

    async def tool_index(self, domain: str | None = None) -> tuple[list[dict], dict[str, str]]:
        """Every visible tool, plus errors of servers that could not be listed."""
        tools, errors = [], {}
        for b in self.backends.values():
            if domain and b.cfg.domain != domain:
                continue
            if b.server is None:
                errors[b.name] = b.cfg.reason or "not running"
                continue
            try:
                listed = await self.list_server_tools(b)
            except Exception as exc:  # noqa: BLE001 - report, keep listing the others
                errors[b.name] = _describe(exc)
                continue
            for t in listed:
                tools.append({"name": b.exposed_name(t.name), "server": b.name, "tool": t.name,
                              "domain": b.cfg.domain, "description": (t.description or "").strip()})
        return tools, errors

    async def invoke(self, server: str, tool: str, args: dict[str, Any]) -> tuple[int, dict]:
        b = self.backends.get(server)
        if b is None:
            return 404, {"error": f"unknown server '{server}' (known: {', '.join(sorted(self.backends))})"}
        if b.server is None:
            return 409, {"error": f"not connected: {server}: {b.cfg.reason}"}
        try:
            result = await asyncio.wait_for(b.server.call_tool(tool, args), b.cfg.timeout)
        except NotFoundError:
            return 404, {"error": f"tool not found: {server}/{tool}"}
        except (ToolError, ValidationError) as exc:
            return 200, {"result": {"content": [{"type": "text", "text": str(exc)}], "isError": True}}
        except TimeoutError:
            return 504, {"error": f"{server}/{tool}: no answer within {b.cfg.timeout:g}s"}
        except Exception as exc:  # noqa: BLE001 - backend failure, not a gateway bug
            log.warning("%s/%s failed: %s", server, tool, _describe(exc))
            return 502, {"error": f"{server}/{tool}: {_describe(exc)}"}
        body: dict[str, Any] = {
            "content": [c.model_dump(mode="json", by_alias=True, exclude_none=True) for c in result.content],
            "isError": bool(result.is_error),
        }
        if result.structured_content is not None:
            body["structuredContent"] = result.structured_content
        return 200, {"result": body}

    async def ready(self) -> dict:
        async with self._ready_lock:
            now = time.monotonic()
            if self._ready and now - self._ready[0] < READY_TTL:
                return self._ready[1]
            servers = {}
            for b in self.backends.values():
                if b.server is None:
                    servers[b.name] = {"domain": b.cfg.domain, "status": "off", "reason": b.cfg.reason}
                    continue
                try:
                    n = len(await self.list_server_tools(b))
                    servers[b.name] = {"domain": b.cfg.domain, "status": "ok", "tools": n}
                except Exception as exc:  # noqa: BLE001
                    servers[b.name] = {"domain": b.cfg.domain, "status": "error", "error": _describe(exc)}
            result = {"member": self.cfg.member,
                      "ready": all(s["status"] != "error" for s in servers.values()),
                      "servers": servers, "collisions": await self.collisions()}
            self._ready = (now, result)
            return result

    async def collisions(self) -> list[str]:
        """Tool names served by two servers of one domain (the first mounted wins)."""
        tools, _ = await self.tool_index()
        seen: dict[tuple[str, str], str] = {}
        out = []
        for t in tools:
            key = (t["domain"], t["name"])
            if key in seen:
                out.append(f"{t['domain']}: '{t['name']}' from {seen[key]} and {t['server']}")
            else:
                seen[key] = t["server"]
        return out

    async def warm_up(self) -> None:
        """List every server once at start: installs npx packages into the cache,
        starts the stdio children and logs what each domain serves."""
        for b in self.backends.values():
            if b.server is None:
                continue
            try:
                n = len(await self.list_server_tools(b))
                log.info("server %s (%s): %d tools", b.name, b.cfg.domain, n)
            except Exception as exc:  # noqa: BLE001
                log.error("server %s (%s) is not answering yet: %s", b.name, b.cfg.domain, _describe(exc))
        for c in await self.collisions():
            log.error("tool name collision, set `namespace` on one of them: %s", c)

    # ------------------------------------------------------------------ app
    def asgi(self):
        domain_apps = {d: s.http_app(path=f"/mcp/{d}", stateless_http=True, json_response=True)
                       for d, s in self.domains.items()}

        async def live(_: Request):
            return JSONResponse({"status": "live", "member": self.cfg.member})

        async def ready(_: Request):
            body = await self.ready()
            return JSONResponse(body, status_code=200 if body["ready"] else 503)

        async def tools(request: Request):
            domain = request.query_params.get("domain")
            items, errors = await self.tool_index(domain)
            return JSONResponse({"tools": items, "errors": errors})

        async def invocations(request: Request):
            try:
                payload = await request.json()
                server, _, tool = str(payload["tool"]).partition("/")
                args = payload.get("args") or {}
                if not server or not tool or not isinstance(args, dict):
                    raise ValueError
            except Exception:  # noqa: BLE001
                return JSONResponse({"error": 'expected {"tool": "<server>/<tool>", "args": {...}}'},
                                    status_code=400)
            status, body = await self.invoke(server, tool, args)
            return JSONResponse(body, status_code=status)

        @asynccontextmanager
        async def lifespan(app):
            async with AsyncExitStack() as stack:
                for sub in domain_apps.values():
                    await stack.enter_async_context(sub.router.lifespan_context(sub))
                warm = asyncio.create_task(self.warm_up())
                try:
                    yield
                finally:
                    warm.cancel()

        core = Starlette(routes=[
            Route("/health/live", live),
            Route("/health/ready", ready),
            Route("/api/v1/tools", tools),
            Route("/api/v1/tool-invocations", invocations, methods=["POST"]),
        ], lifespan=lifespan)
        return BearerAuth(DomainDispatch(core, domain_apps), self.cfg.token)


class DomainDispatch:
    """Send /mcp/<domain>[/...] to that domain's FastMCP app, everything else
    (and the lifespan) to the core app."""

    def __init__(self, core, domain_apps: dict):
        self.core = core
        self.domain_apps = domain_apps

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            parts = scope["path"].split("/")
            if len(parts) >= 3 and parts[1] == "mcp" and parts[2] in self.domain_apps:
                return await self.domain_apps[parts[2]](scope, receive, send)
            if len(parts) >= 2 and parts[1] == "mcp":
                return await JSONResponse(
                    {"error": f"unknown domain; known: {', '.join(sorted(self.domain_apps))}"},
                    status_code=404)(scope, receive, send)
        return await self.core(scope, receive, send)


class BearerAuth:
    """Every route except /health/live requires `Authorization: Bearer <token>`."""

    OPEN = {"/health/live"}

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] not in self.OPEN:
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                return await JSONResponse({"error": "unauthorized"}, status_code=401,
                                          headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        return await self.app(scope, receive, send)


def _describe(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text if len(text) <= 300 else text[:299] + "…"
