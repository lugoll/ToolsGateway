"""API tokens: what MCP clients send as `Authorization: Bearer <token>`.

A token is "tgw_" + 32 random bytes. It is shown once when created; the
database keeps its SHA-256 (enough for a high-entropy value, and fast to look
up) and the first characters so the user can tell tokens apart. A token opens
only its owner's /mcp/<user> and /api/v1/<user>.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..storage.db import Database, now
from .users import User, Users

PREFIX = "tgw_"
LAST_USED_EVERY = timedelta(minutes=5)   # spare the SD card: not every request writes


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Token:
    id: int
    user_id: int
    label: str
    prefix: str
    created: str
    last_used: str | None
    revoked: str | None


class Tokens:
    def __init__(self, db: Database, users: Users):
        self.db = db
        self.users = users

    def create(self, user_id: int, label: str) -> tuple[str, Token]:
        label = label.strip()[:60] or "token"
        token = PREFIX + secrets.token_urlsafe(32)
        cur = self.db.execute(
            "INSERT INTO api_tokens (user_id, label, hash, prefix, created) VALUES (?, ?, ?, ?, ?)",
            (user_id, label, digest(token), token[:10], now()))
        return token, self.get(cur.lastrowid)

    def get(self, token_id: int) -> Token | None:
        row = self.db.one("SELECT * FROM api_tokens WHERE id = ?", (token_id,))
        return None if row is None else Token(row["id"], row["user_id"], row["label"], row["prefix"],
                                              row["created"], row["last_used"], row["revoked"])

    def for_user(self, user_id: int) -> list[Token]:
        rows = self.db.all("SELECT id FROM api_tokens WHERE user_id = ? ORDER BY revoked IS NOT NULL, id DESC",
                           (user_id,))
        return [self.get(r["id"]) for r in rows]

    def revoke(self, user_id: int, token_id: int) -> bool:
        cur = self.db.execute("UPDATE api_tokens SET revoked = ? WHERE id = ? AND user_id = ? AND revoked IS NULL",
                              (now(), token_id, user_id))
        return cur.rowcount == 1

    def authenticate(self, token: str) -> User | None:
        if not token.startswith(PREFIX):
            return None
        row = self.db.one("SELECT id, user_id, last_used FROM api_tokens WHERE hash = ? AND revoked IS NULL",
                          (digest(token),))
        if row is None:
            return None
        user = self.users.get(row["user_id"])
        if user is None or user.disabled:
            return None
        stamp = datetime.now(timezone.utc)
        if not row["last_used"] or stamp - datetime.fromisoformat(row["last_used"]) > LAST_USED_EVERY:
            self.db.execute("UPDATE api_tokens SET last_used = ? WHERE id = ?",
                            (stamp.isoformat(timespec="seconds"), row["id"]))
        return user
