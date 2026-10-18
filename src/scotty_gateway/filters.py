"""Per-server tool allow/deny lists, applied as a FastMCP transform.

The transform sits on the server's own FastMCP instance, so it holds for every
path to a tool: the domain MCP endpoints, the REST endpoint and the CLI.
"""
from __future__ import annotations

import fnmatch
from collections.abc import Sequence

from fastmcp.server.transforms import GetToolNext, Transform
from fastmcp.tools.base import Tool


class ToolFilter(Transform):
    """`allow`: glob patterns a tool must match (None = every tool).
    `deny`: glob patterns that hide a tool; deny wins over allow."""

    def __init__(self, allow: list[str] | None, deny: list[str]):
        self.allow = allow
        self.deny = deny

    def __repr__(self) -> str:
        return f"ToolFilter(allow={self.allow!r}, deny={self.deny!r})"

    def permits(self, name: str) -> bool:
        if any(fnmatch.fnmatchcase(name, p) for p in self.deny):
            return False
        return self.allow is None or any(fnmatch.fnmatchcase(name, p) for p in self.allow)

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [t for t in tools if self.permits(t.name)]

    async def get_tool(self, name: str, call_next: GetToolNext, *, version=None) -> Tool | None:
        if not self.permits(name):
            return None
        return await call_next(name, version=version)
