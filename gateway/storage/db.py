"""The SQLite database: one file in the data directory, WAL mode, numbered migrations.

One connection shared by the whole process behind a lock: requests are short
and few (a household), and tool calls that run in worker threads may write
audit rows too. Migrations are migrations/NNN_*.sql, applied in order and
tracked in PRAGMA user_version.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS = Path(__file__).parent / "migrations"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if self._conn.execute("PRAGMA user_version").fetchone()[0] == 0:
            # Only effective before the first table exists.
            self._conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self.migrate()

    def migrate(self) -> None:
        with self._lock:
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for path in sorted(MIGRATIONS.glob("*.sql")):
                number = int(path.name.split("_", 1)[0])
                if number <= current:
                    continue
                try:
                    self._conn.executescript(f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                                             f"PRAGMA user_version = {number};\nCOMMIT;")
                except sqlite3.Error:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: list) -> None:
        with self.transaction() as conn:
            conn.executemany(sql, rows)

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def get_meta(self, key: str) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
