"""Native mail connector: IMAP and Gmail-API mailboxes as one tool set.
Read, mark, move, draft; never send or delete."""
from __future__ import annotations

from fastmcp import FastMCP

from .. import AccountRuntime, ConnectorContext
from .config import BACKENDS, CAPABILITIES
from .tools import build_server

INTEGRATION = "mail"
CHECK = ("list_mailboxes", "mailboxes")      # "Test connection": tool, result list counted
__all__ = ["BACKENDS", "CAPABILITIES", "CHECK", "INTEGRATION", "build"]


def build(accounts: list[AccountRuntime], ctx: ConnectorContext) -> FastMCP:
    return build_server(accounts, ctx)
