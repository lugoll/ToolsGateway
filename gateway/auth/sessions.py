"""Web UI sessions: a random cookie value, stored as its SHA-256, with a CSRF token.

The cookie is HttpOnly and SameSite=Strict; with an https public URL it is
`__Host-session` and Secure (bound to the exact host, no Domain, path /).
Sessions end after IDLE without a request or after MAX_AGE in any case, at
logout, and when the user's password changes or the user is disabled.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..storage.db import Database
from .users import User, Users

IDLE = timedelta(days=3)
MAX_AGE = timedelta(days=30)
TOUCH_EVERY = timedelta(minutes=5)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


@dataclass(frozen=True)
class Session:
    id: int
    user: User
    csrf: str


class Sessions:
    def __init__(self, db: Database, users: Users, secure: bool):
        self.db = db
        self.users = users
        self.cookie_name = "__Host-session" if secure else "session"
        self.secure = secure

    def create(self, user: User) -> str:
        value = secrets.token_urlsafe(32)
        stamp = _ts(datetime.now(timezone.utc))
        self.db.execute("INSERT INTO sessions (hash, user_id, csrf, created, last_seen) VALUES (?, ?, ?, ?, ?)",
                        (_digest(value), user.id, secrets.token_urlsafe(24), stamp, stamp))
        return value

    def lookup(self, value: str | None) -> Session | None:
        if not value:
            return None
        row = self.db.one("SELECT * FROM sessions WHERE hash = ?", (_digest(value),))
        if row is None:
            return None
        now = datetime.now(timezone.utc)
        last_seen = datetime.fromisoformat(row["last_seen"])
        if now - last_seen > IDLE or now - datetime.fromisoformat(row["created"]) > MAX_AGE:
            self.db.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
            return None
        user = self.users.get(row["user_id"])
        if user is None or user.disabled:
            return None
        if now - last_seen > TOUCH_EVERY:
            self.db.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (_ts(now), row["id"]))
        return Session(row["id"], user, row["csrf"])

    def end(self, value: str | None) -> None:
        if value:
            self.db.execute("DELETE FROM sessions WHERE hash = ?", (_digest(value),))

    def prune(self) -> None:
        cutoff = _ts(datetime.now(timezone.utc) - IDLE)
        oldest = _ts(datetime.now(timezone.utc) - MAX_AGE)
        self.db.execute("DELETE FROM sessions WHERE last_seen < ? OR created < ?", (cutoff, oldest))

    def cookie_args(self) -> dict:
        return {"key": self.cookie_name, "httponly": True, "samesite": "strict", "secure": self.secure,
                "path": "/", "max_age": int(MAX_AGE.total_seconds())}
