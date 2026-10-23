"""Native calendar connector: CalDAV and Google calendars as one tool set.
Read, create, change and (if allowed) delete events; never invite anyone."""
from __future__ import annotations

from fastmcp import FastMCP

from .. import AccountRuntime, ConnectorContext
from .config import BACKENDS, CAPABILITIES
from .tools import build_server

INTEGRATION = "calendar"
CHECK = ("list_calendars", "calendars")      # "Test connection": tool, result list counted
__all__ = ["BACKENDS", "CAPABILITIES", "CHECK", "INTEGRATION", "build"]


def build(accounts: list[AccountRuntime], ctx: ConnectorContext) -> FastMCP:
    return build_server(accounts, ctx)
