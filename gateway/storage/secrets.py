"""Encrypted values in the database: per connection and instance wide.

Values are decrypted only when a connection is built or an OAuth flow needs
them; the UI can set and remove them and sees when and by whom they were set,
never the value. The key check in `meta` makes a wrong or missing master key
fail at start instead of at the first tool call.
"""
from __future__ import annotations

from .crypto import CryptoError, Vault
from .db import Database, now

KEY_CHECK = "tools-gateway key check"


class SecretStore:
    def __init__(self, db: Database, vault: Vault):
        self.db = db
        self.vault = vault

    # ------------------------------------------------------------ key check
    def verify_key(self) -> None:
        """Raise CryptoError unless the configured key opens this database
        (the first start stores the check value)."""
        stored = self.db.get_meta("key_check")
        if stored is None:
            self.db.set_meta("key_check", self.vault.encrypt(KEY_CHECK, "meta:key_check"))
            return
        try:
            ok = self.vault.decrypt(stored, "meta:key_check") == KEY_CHECK
        except CryptoError as exc:
            raise CryptoError(f"GATEWAY_MASTER_KEY does not open this database: {exc}") from None
        if not ok:
            raise CryptoError("GATEWAY_MASTER_KEY does not open this database")

    # ---------------------------------------------------------- connections
    def set(self, connection_id: int, name: str, value: str, by: str) -> None:
        enc = self.vault.encrypt(value, f"secrets:{connection_id}:{name}")
        self.db.execute(
            "INSERT INTO secrets (connection_id, name, value, updated, updated_by) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (connection_id, name) DO UPDATE SET value = excluded.value, "
            "updated = excluded.updated, updated_by = excluded.updated_by",
            (connection_id, name, enc, now(), by))

    def get(self, connection_id: int, name: str) -> str | None:
        row = self.db.one("SELECT value FROM secrets WHERE connection_id = ? AND name = ?", (connection_id, name))
        return None if row is None else self.vault.decrypt(row["value"], f"secrets:{connection_id}:{name}")

    def all(self, connection_id: int) -> dict[str, str]:
        rows = self.db.all("SELECT name, value FROM secrets WHERE connection_id = ?", (connection_id,))
        return {r["name"]: self.vault.decrypt(r["value"], f"secrets:{connection_id}:{r['name']}") for r in rows}

    def describe(self, connection_id: int) -> dict[str, dict]:
        """name -> {updated, updated_by}: what the UI shows instead of the value."""
        rows = self.db.all("SELECT name, updated, updated_by FROM secrets WHERE connection_id = ?", (connection_id,))
        return {r["name"]: {"updated": r["updated"], "updated_by": r["updated_by"]} for r in rows}

    def delete(self, connection_id: int, name: str) -> None:
        self.db.execute("DELETE FROM secrets WHERE connection_id = ? AND name = ?", (connection_id, name))

    # ------------------------------------------------------------- instance
    def set_instance(self, name: str, value: str, by: str) -> None:
        enc = self.vault.encrypt(value, f"instance:{name}")
        self.db.execute(
            "INSERT INTO instance_secrets (name, value, updated, updated_by) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (name) DO UPDATE SET value = excluded.value, updated = excluded.updated, "
            "updated_by = excluded.updated_by", (name, enc, now(), by))

    def get_instance(self, name: str) -> str | None:
        row = self.db.one("SELECT value FROM instance_secrets WHERE name = ?", (name,))
        return None if row is None else self.vault.decrypt(row["value"], f"instance:{name}")

    def describe_instance(self) -> dict[str, dict]:
        rows = self.db.all("SELECT name, updated, updated_by FROM instance_secrets")
        return {r["name"]: {"updated": r["updated"], "updated_by": r["updated_by"]} for r in rows}

    def delete_instance(self, name: str) -> None:
        self.db.execute("DELETE FROM instance_secrets WHERE name = ?", (name,))

    # ------------------------------------------------------------- rotation
    def rotate(self) -> int:
        """Re-encrypt every value not written with the current key; returns how many."""
        changed = 0
        with self.db.transaction() as conn:
            for r in conn.execute("SELECT connection_id, name, value FROM secrets").fetchall():
                if not self.vault.is_current(r["value"]):
                    aad = f"secrets:{r['connection_id']}:{r['name']}"
                    conn.execute("UPDATE secrets SET value = ? WHERE connection_id = ? AND name = ?",
                                 (self.vault.encrypt(self.vault.decrypt(r["value"], aad), aad),
                                  r["connection_id"], r["name"]))
                    changed += 1
            for r in conn.execute("SELECT name, value FROM instance_secrets").fetchall():
                if not self.vault.is_current(r["value"]):
                    aad = f"instance:{r['name']}"
                    conn.execute("UPDATE instance_secrets SET value = ? WHERE name = ?",
                                 (self.vault.encrypt(self.vault.decrypt(r["value"], aad), aad), r["name"]))
                    changed += 1
            check = conn.execute("SELECT value FROM meta WHERE key = 'key_check'").fetchone()
            if check and not self.vault.is_current(check["value"]):
                conn.execute("UPDATE meta SET value = ? WHERE key = 'key_check'",
                             (self.vault.encrypt(KEY_CHECK, "meta:key_check"),))
        return changed
