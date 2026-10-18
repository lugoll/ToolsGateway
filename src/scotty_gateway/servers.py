"""Turn each configured server into its own FastMCP instance.

Proxied servers (stdio, http) become a FastMCP proxy; native ones are built by
their module. Every instance gets the server's ToolFilter and the audit
middleware, then is mounted into its domain's FastMCP server (servers.py ->
app.py). The REST endpoint calls the same instance directly, so both paths see
the same filtered tool set.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fastmcp import FastMCP
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import ProxyClient

from .audit import AuditMiddleware
from .config import GatewayConfig, ServerConfig
from .filters import ToolFilter
from .modules import ModuleContext, load_module

log = logging.getLogger(__name__)


@dataclass
class Backend:
    cfg: ServerConfig
    server: FastMCP | None      # None when the server is not running (cfg.reason says why)

    @property
    def name(self) -> str:
        return self.cfg.name

    def exposed_name(self, tool: str) -> str:
        """The tool's name on the domain endpoint."""
        return f"{self.cfg.namespace}_{tool}" if self.cfg.namespace else tool


def _proxy(cfg: ServerConfig) -> FastMCP:
    if cfg.type == "stdio":
        # keep_alive: one long-lived child process per server; it is respawned
        # on the next call after it exits. Only `env` (plus FastMCP's baseline:
        # HOME, PATH, ...) reaches the child, never the gateway's other secrets.
        transport = StdioTransport(command=cfg.command, args=cfg.args, env=cfg.env or None,
                                   cwd=cfg.cwd, keep_alive=True)
    else:
        transport = StreamableHttpTransport(cfg.url, headers=cfg.headers or None)
    client = ProxyClient(transport, timeout=cfg.timeout, mode=cfg.mode)
    # "raise": a backend that cannot list its tools is an error here, not an
    # empty list ("warn", FastMCP's default, made a dead server look healthy
    # with 0 tools). The domain server that mounts this one still skips it and
    # serves the other servers.
    return create_proxy(client, name=cfg.name, provider_error_strategy="raise")


def _native(cfg: ServerConfig, gw: GatewayConfig) -> FastMCP:
    module = load_module(cfg.module)
    data_dir = gw.data_dir / cfg.name
    data_dir.mkdir(parents=True, exist_ok=True)
    ctx = ModuleContext(server=cfg.name, member=gw.member, data_dir=data_dir,
                        log=logging.getLogger(f"scotty_gateway.{cfg.name}"))
    return module.build(cfg.config, ctx)


def build_backend(cfg: ServerConfig, gw: GatewayConfig) -> Backend:
    if not cfg.enabled:
        log.info("server %s not started (%s)", cfg.name, cfg.reason)
        return Backend(cfg, None)
    try:
        server = _native(cfg, gw) if cfg.type == "native" else _proxy(cfg)
    except Exception as exc:  # noqa: BLE001 - one broken server must not stop the others
        log.error("server %s failed to build: %s", cfg.name, exc)
        cfg.reason = f"failed to start: {exc}"
        return Backend(cfg, None)
    server.add_transform(ToolFilter(cfg.allow, cfg.deny))
    server.add_middleware(AuditMiddleware(cfg.name))
    return Backend(cfg, server)
