"""One audit row per tool call: who called what, how long it took, whether it worked.

Argument *names* are recorded, never values (mail bodies, search terms and
recipients stay out of the log).
"""
from __future__ import annotations

import time

from fastmcp.server.middleware import Middleware

from ..storage.audit import AuditLog


class AuditMiddleware(Middleware):
    """Attached to every integration's FastMCP instance (see registry.py)."""

    def __init__(self, audit: AuditLog, user: str, integration: str, connection: str | None = None):
        self.audit = audit
        self.user = user
        self.integration = integration
        self.connection = connection

    def _record(self, msg, started: float, error: str | None) -> None:
        args = msg.arguments or {}
        detail = {"args": sorted(args.keys())}
        if error:
            detail["error"] = error[:300]
        self.audit.record(self.user, "call", msg.name, integration=self.integration,
                          connection=self.connection or args.get("account"), ok=error is None,
                          ms=round((time.monotonic() - started) * 1000), detail=detail)

    async def on_call_tool(self, context, call_next):
        started = time.monotonic()
        msg = context.message
        try:
            result = await call_next(context)
        except Exception as exc:
            self._record(msg, started, f"{type(exc).__name__}: {exc}")
            raise
        error = None
        if getattr(result, "is_error", False) or getattr(result, "isError", False):
            error = "tool returned an error result"
        self._record(msg, started, error)
        return result
