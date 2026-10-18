"""One JSON line per tool call: who called what, how long it took, whether it worked.

Argument *names* are logged, never values (mail bodies, search terms and
recipients stay out of the log). Lines go to the `scotty_gateway.audit` logger
(stdout) and to <data>/audit.jsonl, rotated at 5 MB with three generations.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import time
from datetime import datetime, timezone
from pathlib import Path

from fastmcp.server.middleware import Middleware

log = logging.getLogger("scotty_gateway.audit")


def setup(data_dir: Path) -> None:
    log.setLevel(logging.INFO)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            data_dir / "audit.jsonl", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    except OSError as exc:
        logging.getLogger(__name__).warning("audit file disabled: %s", exc)
        return
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)


def record(server: str, tool: str, args: dict | None, started: float, error: str | None) -> None:
    log.info(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "server": server,
        "tool": tool,
        "args": sorted((args or {}).keys()),
        "ms": round((time.monotonic() - started) * 1000),
        "ok": error is None,
        **({"error": error[:300]} if error else {}),
    }, ensure_ascii=False))


class AuditMiddleware(Middleware):
    """Attached to every server's FastMCP instance (see servers.py)."""

    def __init__(self, server: str):
        self.server = server

    async def on_call_tool(self, context, call_next):
        started = time.monotonic()
        msg = context.message
        try:
            result = await call_next(context)
        except Exception as exc:
            record(self.server, msg.name, msg.arguments, started, f"{type(exc).__name__}: {exc}")
            raise
        error = None
        if getattr(result, "is_error", False) or getattr(result, "isError", False):
            error = "tool returned an error result"
        record(self.server, msg.name, msg.arguments, started, error)
        return result
