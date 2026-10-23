"""The catalog: YAML templates a user instantiates as connections.

One file per entry in the catalog directory (format: catalog/README.md). An
entry is either `native` (one account of a native connector such as mail; all
of a user's accounts of one integration share one tool set) or `remote` (an MCP
server reached over streamable HTTP and proxied as is). Entries are validated at
start against the connectors they name; a broken file stops the gateway.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..connectors import CONNECTORS, load_connector

SLUG = r"^[a-z][a-z0-9-]{0,31}$"
PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


class CatalogError(ValueError):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldSpec(_Model):
    """One value the UI asks for. `secret` values go to the encrypted store."""
    name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    label: str
    type: Literal["text", "email", "url", "number", "bool", "secret"] = "text"
    required: bool = True
    default: str | int | bool | None = None
    help: str | None = None

    @property
    def secret(self) -> bool:
        return self.type == "secret"


class ToolPolicy(_Model):
    """`deny`: tools hidden by default, the user may enable them.
    `forbid`: tools that are never served (sending, permanent deletion)."""
    deny: list[str] = []
    forbid: list[str] = []


class Capabilities(_Model):
    default: list[str]


class OAuthSpec(_Model):
    """`google`: the instance's Google client with these scopes.
    `mcp`: the MCP authorization flow of the remote server."""
    provider: Literal["google", "mcp"]
    scopes: list[str] = []


class CatalogEntry(_Model):
    id: str = Field(pattern=SLUG)
    label: str
    description: str = ""
    kind: Literal["native", "remote"]
    integration: str = Field(pattern=SLUG)
    backend: str | None = None              # native: connector backend (imap, gmail, ...)
    settings: dict[str, str | int | bool] = {}   # fixed values (host, port, base URL)
    fields: list[FieldSpec] = []
    capabilities: Capabilities | None = None
    oauth: OAuthSpec | None = None
    url: str | None = None                  # remote: fixed URL, else a field named `url`
    headers: dict[str, str] = {}            # remote: values may use {field} placeholders
    tools: ToolPolicy = ToolPolicy()
    timeout: float = 60.0
    help: str | None = None

    @model_validator(mode="after")
    def _check(self) -> CatalogEntry:
        names = [f.name for f in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("field names must be unique")
        if set(names) & set(self.settings):
            raise ValueError("a name is both a fixed setting and a field")
        if self.kind == "remote":
            if self.backend or self.capabilities:
                raise ValueError("remote entries have no backend or capabilities")
            if not self.url and "url" not in names:
                raise ValueError("remote entries need `url` or a field named url")
            known = set(names) | set(self.settings)
            for header, template in self.headers.items():
                unknown = set(PLACEHOLDER.findall(template)) - known
                if unknown:
                    raise ValueError(f"headers.{header}: unknown placeholders {sorted(unknown)}")
        else:
            if self.headers or self.url or self.tools.deny or self.tools.forbid:
                raise ValueError("native entries have no url, headers or tools (capabilities instead)")
        return self

    def field(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)

    @property
    def secret_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if f.secret]


class Catalog:
    def __init__(self, entries: dict[str, CatalogEntry]):
        self.entries = entries

    def get(self, entry_id: str) -> CatalogEntry | None:
        return self.entries.get(entry_id)

    def __iter__(self):
        return iter(sorted(self.entries.values(), key=lambda e: (e.integration, e.label)))

    @property
    def native_integrations(self) -> set[str]:
        return {e.integration for e in self.entries.values() if e.kind == "native"}


def _validate_native(entry: CatalogEntry) -> None:
    if entry.integration not in CONNECTORS:
        raise CatalogError(f"unknown native integration '{entry.integration}' (known: {', '.join(sorted(CONNECTORS))})")
    connector = load_connector(entry.integration)
    if entry.backend not in connector.BACKENDS:
        raise CatalogError(f"backend: one of {', '.join(sorted(connector.BACKENDS))}")
    if entry.capabilities:
        unknown = set(entry.capabilities.default) - set(connector.CAPABILITIES)
        if unknown:
            raise CatalogError(f"capabilities: unknown {sorted(unknown)} (known: {', '.join(connector.CAPABILITIES)})")


def parse_entry(data: object, where: str) -> CatalogEntry:
    try:
        entry = CatalogEntry.model_validate(data)
    except ValidationError as exc:
        raise CatalogError(f"{where}: {exc}") from None
    if entry.kind == "native":
        try:
            _validate_native(entry)
        except CatalogError as exc:
            raise CatalogError(f"{where}: {exc}") from None
    return entry


def load(directory: Path) -> Catalog:
    if not directory.is_dir():
        raise CatalogError(f"catalog directory {directory} does not exist")
    entries: dict[str, CatalogEntry] = {}
    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise CatalogError(f"{path.name}: {exc}") from None
        entry = parse_entry(data, path.name)
        if entry.id != path.stem:
            raise CatalogError(f"{path.name}: id '{entry.id}' must match the file name")
        entries[entry.id] = entry
    remote = {e.integration for e in entries.values() if e.kind == "remote"}
    clash = remote & {e.integration for e in entries.values() if e.kind == "native"}
    if clash:
        raise CatalogError(f"integration names used by native and remote entries: {sorted(clash)}")
    return Catalog(entries)
