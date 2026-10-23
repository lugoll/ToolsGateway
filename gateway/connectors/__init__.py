"""Native connectors: integrations implemented here instead of proxied.

A connector is a package in this directory that exposes

    INTEGRATION: str              the path segment, /mcp/<user>/<INTEGRATION>
    BACKENDS: set[str]            provider protocols it speaks (imap, gmail, ...)
    CAPABILITIES: tuple[str, ...] what an account may allow (read, move, ...)
    CHECK: (tool, key)            "Test connection" calls tool(account=...) and counts result[key]
    build(accounts: list[AccountRuntime], ctx: ConnectorContext) -> FastMCP
        one tool set over all of a user's accounts of this integration; tools
        take an `account` argument where the user has more than one

Register it in CONNECTORS below; catalog entries with `kind: native` and
`integration: <name>` then describe accounts for it. Secrets arrive decrypted
in AccountRuntime.secrets. Every change a tool makes to a mailbox, calendar,
... goes through ctx.record_change, which lands in the audit log.
"""
from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from types import ModuleType

from mcp.types import ToolAnnotations

CONNECTORS = {
    "mail": "gateway.connectors.mail",
    "calendar": "gateway.connectors.calendar",
}


@dataclass
class AccountRuntime:
    """One connection of a native integration, ready to use."""
    name: str                         # connection name, the `account` argument
    label: str
    backend: str
    values: dict                      # catalog settings + the user's field values
    secrets: dict[str, str]           # decrypted secret fields (password, oauth_token, ...)
    capabilities: set[str]
    save_secret: Callable[[str, str], None] = field(default=lambda name, value: None, repr=False)

    def __repr__(self) -> str:        # never print secrets
        return f"AccountRuntime({self.name!r}, backend={self.backend!r})"


@dataclass
class ConnectorContext:
    user: str
    integration: str
    log: logging.Logger
    # (account, action, fields): one entry per change a tool made
    record_change: Callable[[str, str, dict], None] = lambda account, action, fields: None
    # (account) -> where the user fixes a missing authorisation, shown to the agent
    auth_hint: Callable[[str], str] = lambda account: "connect the account in the gateway's web UI"


def hints(*, read_only: bool = False, destructive: bool = False, idempotent: bool = False) -> ToolAnnotations:
    """MCP tool annotations, which clients use to decide when to ask before a call.
    A connector reaches only its own provider, so open_world is always false."""
    return ToolAnnotations(read_only_hint=read_only, destructive_hint=destructive and not read_only,
                           idempotent_hint=idempotent or read_only, open_world_hint=False)


READ = hints(read_only=True)


def load_connector(name: str) -> ModuleType:
    if name not in CONNECTORS:
        raise KeyError(f"unknown connector '{name}' (known: {', '.join(sorted(CONNECTORS))})")
    return importlib.import_module(CONNECTORS[name])
