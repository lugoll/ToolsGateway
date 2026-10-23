"""Connections: catalog entries a user has set up, with their settings and secrets.

Changes notify listeners (the MCP registry rebuilds that user's tool sets).
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from ..catalog import Catalog, CatalogEntry
from .db import Database, now
from .secrets import SecretStore

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class ConnectionSetupError(ValueError):
    pass


@dataclass
class Connection:
    id: int
    user_id: int
    catalog_id: str
    integration: str
    name: str
    label: str
    settings: dict
    capabilities: list[str] | None
    tools: dict = field(default_factory=dict)
    enabled: bool = True
    created: str = ""
    updated: str = ""

    @property
    def enabled_tools(self) -> set[str]:
        return set(self.tools.get("enabled", []))

    @property
    def disabled_tools(self) -> set[str]:
        return set(self.tools.get("disabled", []))


def _conn(row) -> Connection | None:
    if row is None:
        return None
    return Connection(id=row["id"], user_id=row["user_id"], catalog_id=row["catalog_id"],
                      integration=row["integration"], name=row["name"], label=row["label"],
                      settings=json.loads(row["settings"]),
                      capabilities=json.loads(row["capabilities"]) if row["capabilities"] else None,
                      tools=json.loads(row["tools"]), enabled=bool(row["enabled"]),
                      created=row["created"], updated=row["updated"])


def _coerce(entry: CatalogEntry, values: dict) -> dict:
    """Non-secret field values, typed as the catalog says; required ones present."""
    out = {}
    for f in entry.fields:
        if f.secret:
            continue
        raw = values.get(f.name, f.default)
        if raw in (None, ""):
            if f.required:
                raise ConnectionSetupError(f"{f.label} is required")
            continue
        if f.type == "number":
            try:
                raw = int(raw)
            except (TypeError, ValueError):
                raise ConnectionSetupError(f"{f.label}: expected a number") from None
        elif f.type == "bool":
            raw = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "on", "yes")
        else:
            raw = str(raw).strip()
            if f.type == "url" and not raw.startswith(("https://", "http://")):
                raise ConnectionSetupError(f"{f.label}: expected an http(s) URL")
        out[f.name] = raw
    return out


class Connections:
    def __init__(self, db: Database, secrets: SecretStore, catalog: Catalog):
        self.db = db
        self.secrets = secrets
        self.catalog = catalog
        self._listeners: list[Callable[[int], None]] = []

    def on_change(self, listener: Callable[[int], None]) -> None:
        self._listeners.append(listener)

    def _changed(self, user_id: int) -> None:
        for listener in self._listeners:
            listener(user_id)

    # ---------------------------------------------------------------- reads
    def get(self, connection_id: int, user_id: int | None = None) -> Connection | None:
        c = _conn(self.db.one("SELECT * FROM connections WHERE id = ?", (connection_id,)))
        return None if c is None or (user_id is not None and c.user_id != user_id) else c

    def for_user(self, user_id: int) -> list[Connection]:
        return [_conn(r) for r in self.db.all(
            "SELECT * FROM connections WHERE user_id = ? ORDER BY integration, name", (user_id,))]

    # --------------------------------------------------------------- writes
    def create(self, user_id: int, entry: CatalogEntry, name: str, label: str, values: dict,
               secrets: dict[str, str], by: str) -> Connection:
        name = name.strip().lower()
        if not NAME_RE.match(name):
            raise ConnectionSetupError("names are lowercase letters, digits and '-', starting with a letter")
        integration = entry.integration if entry.kind == "native" else name
        if entry.kind == "remote" and name in self.catalog.native_integrations:
            raise ConnectionSetupError(f"'{name}' is the name of a built-in integration; pick another")
        if self.db.one("SELECT 1 FROM connections WHERE user_id = ? AND integration = ? AND name = ?",
                       (user_id, integration, name)):
            raise ConnectionSetupError(f"you already have a connection named '{name}' in {integration}")
        settings = _coerce(entry, values)
        missing = [f.label for f in entry.secret_fields if f.required and not secrets.get(f.name)]
        if missing:
            raise ConnectionSetupError(f"{', '.join(missing)} required")
        stamp = now()
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO connections (user_id, catalog_id, integration, name, label, settings, created, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, entry.id, integration, name, label.strip() or entry.label, json.dumps(settings),
                 stamp, stamp))
            cid = cur.lastrowid
        for f in entry.secret_fields:
            if secrets.get(f.name):
                self.secrets.set(cid, f.name, secrets[f.name], by)
        self._changed(user_id)
        return self.get(cid)

    def update(self, c: Connection, *, label: str | None = None, values: dict | None = None,
               capabilities: list[str] | None = None, tools: dict | None = None,
               enabled: bool | None = None) -> Connection:
        entry = self.catalog.get(c.catalog_id)
        settings = c.settings if values is None or entry is None else _coerce(entry, {**c.settings, **values})
        self.db.execute(
            "UPDATE connections SET label = ?, settings = ?, capabilities = ?, tools = ?, enabled = ?, updated = ? "
            "WHERE id = ?",
            ((label or c.label).strip(), json.dumps(settings),
             json.dumps(sorted(capabilities)) if capabilities is not None else
             (json.dumps(c.capabilities) if c.capabilities is not None else None),
             json.dumps(tools if tools is not None else c.tools),
             int(c.enabled if enabled is None else enabled), now(), c.id))
        self._changed(c.user_id)
        return self.get(c.id)

    def set_secret(self, c: Connection, name: str, value: str, by: str) -> None:
        self.secrets.set(c.id, name, value, by)
        self.db.execute("UPDATE connections SET updated = ? WHERE id = ?", (now(), c.id))
        self._changed(c.user_id)

    def delete_secret(self, c: Connection, name: str) -> None:
        self.secrets.delete(c.id, name)
        self._changed(c.user_id)

    def delete(self, c: Connection) -> None:
        self.db.execute("DELETE FROM connections WHERE id = ?", (c.id,))
        self._changed(c.user_id)

    def effective_capabilities(self, c: Connection) -> set[str]:
        if c.capabilities is not None:
            return set(c.capabilities)
        entry = self.catalog.get(c.catalog_id)
        if entry and entry.capabilities:
            return set(entry.capabilities.default)
        from ..connectors import load_connector
        return set(load_connector(c.integration).CAPABILITIES) if entry and entry.kind == "native" else set()
