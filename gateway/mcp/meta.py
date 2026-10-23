"""The meta tools on /mcp/<user>: find and call every tool of every integration.

For agents that should see four tools instead of all of them. The same tools
remain directly reachable on /mcp/<user>/<integration>; both paths go through
the integration's own FastMCP instance, so filters, capabilities and the audit
log apply either way.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from pydantic import Field

from ..connectors import READ, hints

if TYPE_CHECKING:
    from .registry import UserRuntime

META_TOOLS = ("list_integrations", "search_tools", "describe_tool", "call_tool")


# Words that say nothing about which tool fits ("what is on my schedule"), and
# the everyday words for what the tools call events, tasks and emails.
STOPWORDS = frozenset(
    "a an and are at be by can do does for from get has have how i in is it me my of on or our please show "
    "tell that the there this to up us was we what when where which who with you your "
    "der die das ein eine und ist im in am auf für mit von zu was wann wo wie mein meine ich".split())
SYNONYMS = {
    "schedule": ("calendar", "events"), "agenda": ("calendar", "events"), "termin": ("event",),
    "termine": ("events",), "appointment": ("event",), "appointments": ("events",), "meeting": ("event",),
    "meetings": ("events",), "todo": ("task",), "todos": ("tasks",), "aufgabe": ("task",),
    "aufgaben": ("tasks",), "message": ("email",), "messages": ("emails",), "inbox": ("emails",),
    "add": ("create",), "new": ("create",), "eintragen": ("create",), "anlegen": ("create",),
}


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[\W_]+", text.lower()) if w]


def _hit(word: str, tokens: list[str]) -> bool:
    """Whole words; from four letters on also parts ("emails" ~ "email")."""
    return any(word == t or (len(word) >= 4 and word in t) or (len(t) >= 4 and t in word) for t in tokens)


def rank(query: str, tools: list[dict]) -> list[dict]:
    """Tools by relevance: a query word in the tool or integration name counts 3, in the
    description 1. Filler words are ignored; with nothing left, every tool matches."""
    words = [w for w in _words(query) if w not in STOPWORDS]
    words += [s for w in words for s in SYNONYMS.get(w, ())]
    if not words:
        return list(tools)
    scored = []
    for t in tools:
        name, text = _words(f"{t['integration']} {t['name']}"), _words(t["description"])
        score = sum(3 if _hit(w, name) else 1 if _hit(w, text) else 0 for w in dict.fromkeys(words))
        if score:
            scored.append((score, t))
    return [t for _, t in sorted(scored, key=lambda st: -st[0])]


def build_meta(runtime: UserRuntime) -> FastMCP:
    mcp = FastMCP(f"{runtime.user.name}-gateway", instructions=(
        "Tools of every connected integration (mail, calendar, ...) are reached through these meta tools: "
        "list_integrations, then search_tools or describe_tool, then call_tool."))

    @mcp.tool(annotations=READ)
    async def list_integrations() -> dict:
        """The connected integrations, their accounts and whether they are working."""
        status = await runtime.status()
        return {"integrations": [{"integration": name, **info} for name, info in status["integrations"].items()]}

    @mcp.tool(annotations=READ)
    async def search_tools(query: Annotated[str, Field(description="words describing the task, e.g. 'unread mail'")],
                           integration: Annotated[str | None, Field(description="limit to one integration")] = None,
                           limit: Annotated[int, Field(ge=1, le=50)] = 10) -> dict:
        """Find tools by name and description. Returns names and descriptions; get the
        arguments with describe_tool."""
        tools, errors = await runtime.tool_index(integration)
        hits = [{"integration": t["integration"], "tool": t["name"], "description": t["description"]}
                for t in rank(query, tools)[:limit]]
        return {"tools": hits, **({"unavailable": errors} if errors else {})}

    @mcp.tool(annotations=READ)
    async def describe_tool(integration: str, tool: str) -> dict:
        """Description and JSON schema of a tool's arguments."""
        tools, errors = await runtime.tool_index(integration)
        if integration in errors:
            raise ToolError(f"{integration} is not available: {errors[integration]}")
        for t in tools:
            if t["name"] == tool:
                return {"integration": integration, "tool": tool, "description": t["description"],
                        "input_schema": t["input_schema"]}
        raise ToolError(f"tool not found: {integration}/{tool}")

    @mcp.tool(annotations=hints(destructive=True))
    async def call_tool(integration: str, tool: str,
                        arguments: Annotated[dict[str, Any] | None, Field(
                            description="the tool's arguments as described by describe_tool")] = None) -> ToolResult:
        """Call a tool of an integration and return its result unchanged."""
        return await runtime.call(integration, tool, arguments or {})

    return mcp
