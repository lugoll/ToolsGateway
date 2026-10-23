"""The audit log: tool calls, changes tools made, and account administration.

Bounded for a Raspberry Pi: rows are buffered and written in batches (every few
seconds, or when the buffer fills), and pruned to GATEWAY_AUDIT_DAYS and
GATEWAY_AUDIT_MAX_ROWS at start and daily; freed pages go back to the file
system with incremental vacuum. Argument values are never recorded, only
names; changes carry what a person needs to find the item again (sender and
subject of a moved mail, title of an event).
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import Database, now

log = logging.getLogger("gateway.audit")

FLUSH_AT = 50


@dataclass
class AuditEntry:
    id: int
    ts: str
    user: str
    kind: str
    integration: str | None
    connection: str | None
    action: str
    ok: bool
    ms: int | None
    detail: dict


class AuditLog:
    def __init__(self, db: Database, days: int = 90, max_rows: int = 100_000):
        self.db = db
        self.days = days
        self.max_rows = max_rows
        self._buffer: list[tuple] = []
        self._lock = threading.Lock()

    def record(self, user: str, kind: str, action: str, *, integration: str | None = None,
               connection: str | None = None, ok: bool = True, ms: int | None = None,
               detail: dict | None = None) -> None:
        detail = detail or {}
        entry = {"user": user, "kind": kind, "integration": integration, "connection": connection,
                 "action": action, "ok": ok, **({"ms": ms} if ms is not None else {}), **detail}
        level = logging.WARNING if detail.get("destination_kind") or not ok else logging.INFO
        log.log(level, json.dumps({k: v for k, v in entry.items() if v is not None}, ensure_ascii=False, default=str))
        row = (now(), user, kind, integration, connection, action, int(ok), ms,
               json.dumps(detail, ensure_ascii=False, default=str) if detail else None)
        with self._lock:
            self._buffer.append(row)
            full = len(self._buffer) >= FLUSH_AT
        if full:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            rows, self._buffer = self._buffer, []
        if rows:
            self.db.executemany(
                "INSERT INTO audit (ts, user, kind, integration, connection, action, ok, ms, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def prune(self) -> int:
        self.flush()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.days)).isoformat(timespec="seconds")
        removed = self.db.execute("DELETE FROM audit WHERE ts < ?", (cutoff,)).rowcount
        row = self.db.one("SELECT id FROM audit ORDER BY id DESC LIMIT 1 OFFSET ?", (self.max_rows,))
        if row:
            removed += self.db.execute("DELETE FROM audit WHERE id <= ?", (row["id"],)).rowcount
        if removed:
            self.db.execute("PRAGMA incremental_vacuum")
        return removed

    def query(self, user: str | None = None, kind: str | None = None, integration: str | None = None,
              before: int | None = None, limit: int = 100) -> list[AuditEntry]:
        self.flush()
        where, params = [], []
        for column, value in (("user", user), ("kind", kind), ("integration", integration)):
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        if before:
            where.append("id < ?")
            params.append(before)
        sql = "SELECT * FROM audit" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY id DESC LIMIT ?"
        rows = self.db.all(sql, (*params, limit))
        return [AuditEntry(r["id"], r["ts"], r["user"], r["kind"], r["integration"], r["connection"], r["action"],
                           bool(r["ok"]), r["ms"], json.loads(r["detail"]) if r["detail"] else {}) for r in rows]
