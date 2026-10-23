"""Users: an admin manages accounts, every user manages their own connections and tokens.

Names are URL slugs (/mcp/<name>). The first admin comes from
GATEWAY_ADMIN_USER / GATEWAY_ADMIN_PASSWORD and is created only while no admin
exists, so changing the variables later never overwrites a password.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..storage.db import Database, now
from . import passwords

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
ROLES = ("admin", "user")

log = logging.getLogger(__name__)


class UserError(ValueError):
    pass


@dataclass(frozen=True)
class User:
    id: int
    name: str
    display_name: str
    role: str
    disabled: bool

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def title(self) -> str:
        return self.display_name or self.name


def _user(row) -> User | None:
    if row is None:
        return None
    return User(id=row["id"], name=row["name"], display_name=row["display_name"], role=row["role"],
                disabled=bool(row["disabled"]))


class Users:
    def __init__(self, db: Database):
        self.db = db

    def get(self, user_id: int) -> User | None:
        return _user(self.db.one("SELECT * FROM users WHERE id = ?", (user_id,)))

    def by_name(self, name: str) -> User | None:
        return _user(self.db.one("SELECT * FROM users WHERE name = ?", (name,)))

    def all(self) -> list[User]:
        return [_user(r) for r in self.db.all("SELECT * FROM users ORDER BY name")]

    def create(self, name: str, password: str, role: str = "user", display_name: str = "") -> User:
        if not NAME_RE.match(name):
            raise UserError("names are 2-32 lowercase letters, digits and '-', starting with a letter")
        if role not in ROLES:
            raise UserError(f"role: one of {', '.join(ROLES)}")
        if self.by_name(name):
            raise UserError(f"user '{name}' exists")
        hashed = passwords.hash_password(password)
        cur = self.db.execute(
            "INSERT INTO users (name, display_name, role, password_hash, created) VALUES (?, ?, ?, ?, ?)",
            (name, display_name.strip(), role, hashed, now()))
        return self.get(cur.lastrowid)

    def authenticate(self, name: str, password: str) -> User | None:
        row = self.db.one("SELECT * FROM users WHERE name = ?", (name,))
        stored = row["password_hash"] if row else None
        if not passwords.verify(stored, password) or row["disabled"]:
            return None
        if passwords.needs_rehash(stored):
            self.db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                            (passwords.hash_password(password), row["id"]))
        return _user(row)

    def set_password(self, user_id: int, password: str) -> None:
        self.db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                        (passwords.hash_password(password), user_id))
        # A new password ends every session of that user.
        self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def update(self, user_id: int, *, display_name: str | None = None, role: str | None = None,
               disabled: bool | None = None) -> None:
        user = self.get(user_id)
        if user is None:
            raise UserError("no such user")
        if role is not None and role not in ROLES:
            raise UserError(f"role: one of {', '.join(ROLES)}")
        demoted = (role == "user" or disabled) and user.is_admin
        if demoted and self.admin_count(excluding=user_id) == 0:
            raise UserError("the last active admin cannot be demoted or disabled")
        self.db.execute(
            "UPDATE users SET display_name = COALESCE(?, display_name), role = COALESCE(?, role), "
            "disabled = COALESCE(?, disabled) WHERE id = ?",
            (display_name, role, None if disabled is None else int(disabled), user_id))
        if disabled:
            self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def delete(self, user_id: int) -> None:
        user = self.get(user_id)
        if user and user.is_admin and self.admin_count(excluding=user_id) == 0:
            raise UserError("the last active admin cannot be deleted")
        self.db.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def admin_count(self, excluding: int | None = None) -> int:
        row = self.db.one("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND disabled = 0 AND id != ?",
                          (excluding or 0,))
        return row["n"]

    def bootstrap_admin(self, name: str | None, password: str | None) -> User | None:
        if self.admin_count() > 0:
            return None
        if not name or not password:
            log.warning("no admin exists: set GATEWAY_ADMIN_USER and GATEWAY_ADMIN_PASSWORD")
            return None
        existing = self.by_name(name)
        if existing:
            # The named user exists but is no (active) admin: promote, keep the password.
            self.db.execute("UPDATE users SET role = 'admin', disabled = 0 WHERE id = ?", (existing.id,))
            log.warning("promoted existing user %s to admin (no active admin was left)", name)
            return self.get(existing.id)
        user = self.create(name, password, role="admin")
        log.info("created admin %s from GATEWAY_ADMIN_USER", name)
        return user
