"""Per-user runtimes: every integration of one user as FastMCP servers, plus the meta server.

A runtime is built on the first request for that user and rebuilt after any
change to their connections (the UI calls Connections.*, which notifies
`invalidate`). The new runtime is started and swapped in; the old one keeps
serving requests already in flight and is stopped after a grace period. The
endpoints are stateless (no MCP session ids), so a rebuild never strands a
client.

Integrations:
    native   all of the user's connections of one connector (mail: every
             mailbox) become the accounts of one tool set built by the connector
    remote   one proxy per connection, with the catalog's tool policy and the
             user's toggles applied as a ToolFilter
One broken connection never takes the others down: the integration reports
why it is not running and the rest keep serving.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError
from pydantic import ValidationError

from ..auth.users import User, Users
from ..catalog import Catalog
from ..connectors import AccountRuntime, ConnectorContext, load_connector
from ..outbound.oauth.google import GoogleAuthError, GoogleOAuth, authorized_user_info
from ..outbound.oauth.mcp import McpOAuth
from ..outbound.remote_mcp import build_proxy, render_headers
from ..storage.audit import AuditLog
from ..storage.connections import Connection, Connections
from ..storage.crypto import CryptoError
from .audit import AuditMiddleware
from .filters import ToolFilter

log = logging.getLogger(__name__)

LIST_TIMEOUT = 45.0
STATUS_TTL = 30.0
TOOLS_TTL = 300.0                               # remote tool lists, see list_tools
GRACE = 30.0


def describe(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text if len(text) <= 300 else text[:299] + "…"


@dataclass
class Integration:
    name: str
    kind: str                                   # native | remote
    connections: list[Connection]
    server: FastMCP | None = None
    reason: str | None = None                   # why server is None
    timeout: float = 60.0
    filter: ToolFilter | None = None            # remote only
    raw: FastMCP | None = None                  # remote only: the proxy before the filter (UI tool list)


@dataclass
class UserRuntime:
    user: User
    integrations: dict[str, Integration]
    path_prefix: str = "/mcp"
    meta: FastMCP | None = None
    apps: dict[str, Any] = field(default_factory=dict)      # "" = meta, else integration
    _stop: asyncio.Event | None = None
    _task: asyncio.Task | None = None
    _status: tuple[float, dict] | None = None
    _tools: dict[str, tuple[float, list]] = field(default_factory=dict)

    # ------------------------------------------------------------ lifecycle
    def _build_apps(self) -> None:
        base = f"{self.path_prefix}/{self.user.name}"
        if self.meta is not None:
            self.apps[""] = self.meta.http_app(path=base, stateless_http=True, json_response=True)
        for name, integ in self.integrations.items():
            if integ.server is not None:
                self.apps[name] = integ.server.http_app(path=f"{base}/{name}", stateless_http=True,
                                                        json_response=True)

    async def start(self) -> None:
        """Enter every app's lifespan in one long-lived task (anyio cancel scopes
        must be entered and left by the same task)."""
        self._build_apps()
        started, self._stop = asyncio.Event(), asyncio.Event()
        failure: list[BaseException] = []

        async def run():
            try:
                async with AsyncExitStack() as stack:
                    for app in self.apps.values():
                        await stack.enter_async_context(app.router.lifespan_context(app))
                    started.set()
                    await self._stop.wait()
            except BaseException as exc:  # noqa: BLE001 - surfaced to start()
                failure.append(exc)
                started.set()
                raise

        self._task = asyncio.create_task(run(), name=f"runtime-{self.user.name}")
        await started.wait()
        if failure:
            raise failure[0]

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except Exception as exc:  # noqa: BLE001
                log.warning("runtime %s stopped with %s", self.user.name, describe(exc))

    # ---------------------------------------------------------------- tools
    async def list_tools(self, integ: Integration, fresh: bool = False) -> list:
        """The integration's visible tools. A remote server's list is kept for TOOLS_TTL:
        every meta call needs it and each fetch is a round trip upstream. Any change to
        the user's connections or toggles builds a new runtime, so the cache never
        outlives a configuration; the TTL only covers upstream adding or removing tools."""
        if integ.kind != "remote":
            return list(await asyncio.wait_for(integ.server.list_tools(), LIST_TIMEOUT))
        now = asyncio.get_running_loop().time()
        cached = self._tools.get(integ.name)
        if not fresh and cached and now - cached[0] < TOOLS_TTL:
            return cached[1]
        listed = list(await asyncio.wait_for(integ.server.list_tools(), LIST_TIMEOUT))
        self._tools[integ.name] = (now, listed)
        return listed

    async def tool_index(self, integration: str | None = None) -> tuple[list[dict], dict[str, str]]:
        """Every visible tool, plus errors of integrations that could not be listed."""
        tools, errors = [], {}
        for integ in self.integrations.values():
            if integration and integ.name != integration:
                continue
            if integ.server is None:
                errors[integ.name] = integ.reason or "not running"
                continue
            try:
                listed = await self.list_tools(integ)
            except Exception as exc:  # noqa: BLE001 - report, keep listing the others
                errors[integ.name] = describe(exc)
                continue
            for t in listed:
                tools.append({"integration": integ.name, "name": t.name,
                              "description": (t.description or "").strip(), "input_schema": t.parameters})
        return tools, errors

    async def call(self, integration: str, tool: str, args: dict[str, Any]):
        """Call a tool; raises ToolError with a message fit for the agent."""
        integ = self.integrations.get(integration)
        if integ is None:
            raise ToolError(f"unknown integration '{integration}' (known: {', '.join(sorted(self.integrations))})")
        if integ.server is None:
            raise ToolError(f"{integration} is not connected: {integ.reason}")
        try:
            return await asyncio.wait_for(integ.server.call_tool(tool, args), integ.timeout)
        except NotFoundError:
            raise ToolError(f"tool not found: {integration}/{tool}") from None
        except TimeoutError:
            raise ToolError(f"{integration}/{tool}: no answer within {integ.timeout:g}s") from None

    async def invoke(self, integration: str, tool: str, args: dict[str, Any]) -> tuple[int, dict]:
        """REST shape of `call`: (status, body)."""
        integ = self.integrations.get(integration)
        if integ is None:
            return 404, {"error": f"unknown integration '{integration}' "
                                  f"(known: {', '.join(sorted(self.integrations))})"}
        if integ.server is None:
            return 409, {"error": f"not connected: {integration}: {integ.reason}"}
        try:
            result = await asyncio.wait_for(integ.server.call_tool(tool, args), integ.timeout)
        except NotFoundError:
            return 404, {"error": f"tool not found: {integration}/{tool}"}
        except (ToolError, ValidationError) as exc:
            return 200, {"result": {"content": [{"type": "text", "text": str(exc)}], "isError": True}}
        except TimeoutError:
            return 504, {"error": f"{integration}/{tool}: no answer within {integ.timeout:g}s"}
        except Exception as exc:  # noqa: BLE001 - backend failure, not a gateway bug
            log.warning("%s/%s/%s failed: %s", self.user.name, integration, tool, describe(exc))
            return 502, {"error": f"{integration}/{tool}: {describe(exc)}"}
        body: dict[str, Any] = {
            "content": [c.model_dump(mode="json", by_alias=True, exclude_none=True) for c in result.content],
            "isError": bool(getattr(result, "is_error", False)),
        }
        if result.structured_content is not None:
            body["structuredContent"] = result.structured_content
        return 200, {"result": body}

    async def status(self, fresh: bool = False) -> dict:
        """Per integration: running with n tools, off (why) or error. Cached briefly."""
        now = asyncio.get_running_loop().time()
        if not fresh and self._status and now - self._status[0] < STATUS_TTL:
            return self._status[1]
        out = {}
        for integ in self.integrations.values():
            accounts = [c.name for c in integ.connections]
            if integ.server is None:
                out[integ.name] = {"kind": integ.kind, "status": "off", "reason": integ.reason, "accounts": accounts}
                continue
            try:
                n = len(await self.list_tools(integ, fresh))
                out[integ.name] = {"kind": integ.kind, "status": "ok", "tools": n, "accounts": accounts}
            except Exception as exc:  # noqa: BLE001
                out[integ.name] = {"kind": integ.kind, "status": "error", "error": describe(exc),
                                   "accounts": accounts}
        result = {"user": self.user.name, "ready": all(s["status"] != "error" for s in out.values()),
                  "integrations": out}
        self._status = (now, result)
        return result


class Registry:
    def __init__(self, users: Users, connections: Connections, catalog: Catalog, audit: AuditLog,
                 public_url: str = "", google_oauth: GoogleOAuth | None = None, mcp_oauth: McpOAuth | None = None):
        self.users = users
        self.connections = connections
        self.catalog = catalog
        self.audit = audit
        self.public_url = public_url
        self.google_oauth = google_oauth
        self.mcp_oauth = mcp_oauth
        self._runtimes: dict[int, UserRuntime] = {}
        self._stale: set[int] = set()
        self._locks: dict[int, asyncio.Lock] = {}
        self._retiring: set[asyncio.Task] = set()
        connections.on_change(self.invalidate)

    def invalidate(self, user_id: int) -> None:
        self._stale.add(user_id)

    async def get(self, user: User) -> UserRuntime:
        current = self._runtimes.get(user.id)
        if current is not None and user.id not in self._stale and current.user == user:
            return current
        lock = self._locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            current = self._runtimes.get(user.id)
            if current is not None and user.id not in self._stale and current.user == user:
                return current
            self._stale.discard(user.id)
            runtime = self.build(user)
            await runtime.start()
            self._runtimes[user.id] = runtime
            if current is not None:
                self._retire(current)
            return runtime

    def _retire(self, runtime: UserRuntime) -> None:
        async def later():
            await asyncio.sleep(GRACE)
            await runtime.stop()
        task = asyncio.create_task(later())
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    async def close(self) -> None:
        for task in list(self._retiring):
            task.cancel()
        for runtime in self._runtimes.values():
            await runtime.stop()
        self._runtimes.clear()

    # ------------------------------------------------------------ web UI
    async def test_connection(self, user: User, c: Connection) -> tuple[bool, str]:
        """What "Test connection" shows: a real round trip to the provider."""
        runtime = await self.get(user)
        integ = runtime.integrations.get(c.integration)
        if integ is None or c.name not in [x.name for x in integ.connections]:
            return False, "the connection is disabled"
        if integ.server is None:
            return False, integ.reason or "not running"
        try:
            if integ.kind == "remote":
                n = len(await runtime.list_tools(integ, fresh=True))
                return True, f"connected, {n} tools served"
            tool, key = load_connector(c.integration).CHECK
            result = await asyncio.wait_for(
                integ.server.call_tool(tool, {"account": c.name}, run_middleware=False), integ.timeout)
            items = (result.structured_content or {}).get(key, [])
            return True, f"connected, {len(items)} {key}"
        except Exception as exc:  # noqa: BLE001 - shown to the user
            return False, describe(exc)

    async def remote_tools(self, user: User, c: Connection) -> tuple[list[dict], str | None]:
        """Every upstream tool with its state (on / off / forbidden), for the toggles."""
        runtime = await self.get(user)
        integ = runtime.integrations.get(c.integration)
        if integ is None or integ.raw is None:
            return [], (integ.reason if integ else "the connection is disabled")
        try:
            listed = await asyncio.wait_for(integ.raw.list_tools(), LIST_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            return [], describe(exc)
        return [{"name": t.name, "description": (t.description or "").strip(), "state": integ.filter.state(t.name)}
                for t in sorted(listed, key=lambda t: t.name)], None

    # ------------------------------------------------------------- building
    def auth_hint(self, account: str) -> str:
        where = self.public_url or "the gateway's web UI"
        return f"open {where}, Connections, '{account}', and connect it"

    def build(self, user: User) -> UserRuntime:
        groups: dict[str, Integration] = {}
        for c in self.connections.for_user(user.id):
            if not c.enabled:
                continue
            entry = self.catalog.get(c.catalog_id)
            kind = entry.kind if entry else "remote"
            integ = groups.setdefault(c.integration, Integration(c.integration, kind, []))
            integ.connections.append(c)
        for integ in groups.values():
            try:
                if integ.kind == "native":
                    self._build_native(user, integ)
                else:
                    self._build_remote(user, integ)
            except Exception as exc:  # noqa: BLE001 - one broken integration must not stop the others
                log.error("%s/%s failed to build: %s", user.name, integ.name, describe(exc))
                integ.server, integ.reason = None, f"failed to start: {describe(exc)}"
        runtime = UserRuntime(user=user, integrations=groups)
        from .meta import build_meta
        runtime.meta = build_meta(runtime)
        return runtime

    def _secrets(self, c: Connection) -> dict[str, str]:
        try:
            return self.connections.secrets.all(c.id)
        except CryptoError as exc:
            raise RuntimeError(f"secrets of '{c.name}' cannot be decrypted: {exc}") from None

    def _google_token(self, stored: str) -> str | None:
        """The stored refresh token plus the instance's current Google client;
        None (= needs authorisation) while the admin has not set the client up."""
        if self.google_oauth is None:
            return None
        try:
            client_id, client_secret = self.google_oauth.client()
        except GoogleAuthError:
            return None
        return authorized_user_info(stored, client_id, client_secret)

    def _build_native(self, user: User, integ: Integration) -> None:
        connector = load_connector(integ.name)
        accounts = []
        for c in integ.connections:
            entry = self.catalog.get(c.catalog_id)
            if entry is None:
                log.error("%s/%s: catalog entry %s is gone", user.name, c.name, c.catalog_id)
                continue
            secrets = self._secrets(c)
            if entry.oauth and entry.oauth.provider == "google" and "oauth_token" in secrets:
                secrets["oauth_token"] = self._google_token(secrets["oauth_token"])
            accounts.append(AccountRuntime(
                name=c.name, label=c.label, backend=entry.backend, values={**entry.settings, **c.settings},
                secrets={k: v for k, v in secrets.items() if v is not None},
                capabilities=self.connections.effective_capabilities(c),
                save_secret=lambda name, value, c=c: self.connections.secrets.set(c.id, name, value, "gateway")))
        if not accounts:
            integ.reason = "no usable account"
            return
        ctx = ConnectorContext(
            user=user.name, integration=integ.name, log=logging.getLogger(f"gateway.{user.name}.{integ.name}"),
            record_change=lambda account, action, fields: self.audit.record(
                user.name, "change", action, integration=integ.name, connection=account, detail=fields),
            auth_hint=self.auth_hint)
        server = connector.build(accounts, ctx)
        server.add_middleware(AuditMiddleware(self.audit, user.name, integ.name))
        integ.server = server

    def _build_remote(self, user: User, integ: Integration) -> None:
        (c,) = integ.connections                 # remote: integration == connection
        entry = self.catalog.get(c.catalog_id)
        if entry is None:
            integ.reason = f"catalog entry '{c.catalog_id}' no longer exists"
            return
        values = {**entry.settings, **c.settings}
        secrets = self._secrets(c)
        url = entry.url or values.get("url")
        auth = None
        if entry.oauth is not None:
            if self.mcp_oauth is None:
                integ.reason = "OAuth for remote servers is not configured"
                return
            auth = self.mcp_oauth.runtime_auth(url, c.id, entry.oauth.scopes)
            if auth is None:
                integ.reason = f"not authorised yet: {self.auth_hint(c.name)}"
                return
        integ.timeout = entry.timeout
        integ.filter = ToolFilter(entry.tools.forbid, entry.tools.deny, c.enabled_tools, c.disabled_tools)
        integ.raw = build_proxy(f"{user.name}-{c.name}-upstream", url, render_headers(entry, values, secrets),
                                entry.timeout, auth)
        server = FastMCP(f"{user.name}-{c.name}")
        server.provider_error_strategy = "raise"      # a dead upstream is an error, not 0 tools
        server.mount(integ.raw)
        server.add_transform(integ.filter)
        server.add_middleware(AuditMiddleware(self.audit, user.name, integ.name, c.name))
        integ.server = server
