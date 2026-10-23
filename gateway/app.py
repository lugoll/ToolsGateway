"""Wires the gateway together and serves it as one ASGI app.

    /mcp/..., /api/v1/...   MCP clients and scripts, bearer tokens (mcp/routing.py)
    /health/live            the process is up (no auth; Docker healthcheck)
    /health/ready           database and catalog are usable (no auth, no user detail)
    everything else         the web UI (web/), session cookies
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import catalog as catalog_mod
from .auth.sessions import Sessions
from .auth.tokens import Tokens
from .auth.users import Users
from .catalog import Catalog
from .mcp.registry import Registry
from .outbound.oauth.google import GoogleOAuth
from .outbound.oauth.mcp import McpOAuth
from .mcp.routing import McpRouter
from .settings import Settings
from .storage.audit import AuditLog
from .storage.connections import Connections
from .storage.crypto import Vault
from .storage.db import Database
from .storage.secrets import SecretStore

log = logging.getLogger(__name__)

FLUSH_EVERY = 5.0
PRUNE_EVERY = 24 * 3600.0


@dataclass
class Services:
    settings: Settings
    db: Database
    secrets: SecretStore
    catalog: Catalog
    users: Users
    tokens: Tokens
    connections: Connections
    audit: AuditLog
    registry: Registry
    google_oauth: GoogleOAuth
    mcp_oauth: McpOAuth

    @classmethod
    def open(cls, settings: Settings) -> Services:
        db = Database(settings.db_path)
        secrets = SecretStore(db, Vault(settings.master_key, settings.master_key_old))
        secrets.verify_key()
        catalog = catalog_mod.load(settings.catalog_dir)
        users = Users(db)
        connections = Connections(db, secrets, catalog)
        audit = AuditLog(db, settings.audit_days, settings.audit_max_rows)
        google_oauth = GoogleOAuth(secrets)
        mcp_oauth = McpOAuth(secrets, settings.public_url)
        registry = Registry(users, connections, catalog, audit, public_url=settings.public_url,
                            google_oauth=google_oauth, mcp_oauth=mcp_oauth)
        return cls(settings, db, secrets, catalog, users, Tokens(db, users), connections, audit, registry,
                   google_oauth, mcp_oauth)


class Dispatch:
    """/mcp and /api/v1 to the MCP router, everything else (and the lifespan) to the core app."""

    def __init__(self, core, mcp):
        self.core = core
        self.mcp = mcp

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope["path"]
            if path == "/mcp" or path.startswith(("/mcp/", "/api/v1/")):
                return await self.mcp(scope, receive, send)
        return await self.core(scope, receive, send)


def create_app(services: Services):
    s = services

    async def live(_: Request):
        return JSONResponse({"status": "live"})

    async def ready(_: Request):
        try:
            s.db.one("SELECT 1")
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ready": False, "error": str(exc)}, status_code=503)
        return JSONResponse({"ready": True, "catalog": len(s.catalog.entries)})

    async def housekeeping():
        last_prune = 0.0
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(FLUSH_EVERY)
            try:
                s.audit.flush()
                if loop.time() - last_prune > PRUNE_EVERY:
                    Sessions(s.db, s.users, s.settings.secure_cookies).prune()
                    removed = s.audit.prune()
                    last_prune = loop.time()
                    if removed:
                        log.info("audit: pruned %d entries", removed)
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                log.error("audit housekeeping failed: %s", exc)

    @asynccontextmanager
    async def lifespan(app):
        s.users.bootstrap_admin(s.settings.admin_user, s.settings.admin_password)
        task = asyncio.create_task(housekeeping())
        try:
            yield
        finally:
            task.cancel()
            await s.registry.close()
            s.audit.flush()

    routes = [Route("/health/live", live), Route("/health/ready", ready)]
    from .web import create_web
    core = create_web(s, routes=routes, lifespan=lifespan)
    return Dispatch(core, McpRouter(s.registry, s.tokens))


def build(settings: Settings | None = None):
    return create_app(Services.open(settings or Settings.from_env()))
