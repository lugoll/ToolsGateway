"""Which tools of a remote server are served, applied as a FastMCP transform.

The catalog gives two glob lists: `forbid` (never served: sending, permanent
deletion) and `deny` (off by default). The user's toggles on the connection
override `deny` in both directions but never `forbid`. The transform sits on
the server's own FastMCP instance, so it holds for every path to a tool: the
integration endpoint, the meta tools and the REST endpoint.
"""
from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Sequence

from fastmcp.server.transforms import GetToolNext, Transform
from fastmcp.tools.base import Tool


def _matches(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


class ToolFilter(Transform):
    def __init__(self, forbid: list[str], deny: list[str], enabled: set[str] = frozenset(),
                 disabled: set[str] = frozenset()):
        self.forbid = list(forbid)
        self.deny = list(deny)
        self.enabled = set(enabled)
        self.disabled = set(disabled)

    def __repr__(self) -> str:
        return f"ToolFilter(forbid={self.forbid!r}, deny={self.deny!r})"

    def state(self, name: str) -> str:
        """'forbidden', 'off' or 'on': what the UI shows next to a tool."""
        if _matches(name, self.forbid):
            return "forbidden"
        if name in self.disabled:
            return "off"
        if name in self.enabled:
            return "on"
        return "off" if _matches(name, self.deny) else "on"

    def permits(self, name: str) -> bool:
        return self.state(name) == "on"

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [t for t in tools if self.permits(t.name)]

    async def get_tool(self, name: str, call_next: GetToolNext, *, version=None) -> Tool | None:
        if not self.permits(name):
            return None
        return await call_next(name, version=version)
