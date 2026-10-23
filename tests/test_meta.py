"""search_tools ranking over the real native tool descriptions plus a few of Todoist's."""
import logging

import pytest

from gateway.connectors import ConnectorContext
from gateway.connectors import calendar as calendar_connector
from gateway.connectors import mail as mail_connector
from gateway.mcp.meta import rank

TODOIST = {
    "add-tasks": "Add one or more tasks to a project, section, or parent. Supports assignment to project collaborators.",
    "find-tasks-by-date": "Get tasks by date range, or today's tasks plus overdue ones.",
    "user-info": "Get comprehensive user information including user ID, full name, email, timezone with current "
                 "local time, week start day preferences, current week dates, daily/weekly goal progress, and "
                 "user plan (Free/Pro/Business).",
}


@pytest.fixture(scope="module")
async def tools():
    out = []
    for name, connector in (("mail", mail_connector), ("calendar", calendar_connector)):
        server = connector.build([], ConnectorContext("t", name, logging.getLogger("t")))
        out += [{"integration": name, "name": t.name, "description": t.description}
                for t in await server.list_tools()]
    return out + [{"integration": "todoist", "name": n, "description": d} for n, d in TODOIST.items()]


@pytest.mark.parametrize("query, first", [
    ("what is on my schedule tomorrow", ("calendar", "list_events")),
    ("unread mail", ("mail", "list_emails")),
    ("draft a reply", ("mail", "save_draft")),
    ("add a todo", ("todoist", "add-tasks")),
    ("move this email to the archive", ("mail", "move_email")),
    ("Termin eintragen", ("calendar", "create_event")),
    ("add an appointment on friday", ("calendar", "create_event")),
])
async def test_rank_puts_the_fitting_tool_first(tools, query, first):
    ranked = rank(query, tools)
    assert (ranked[0]["integration"], ranked[0]["name"]) == first, [t["name"] for t in ranked[:5]]


async def test_rank_ignores_filler_words(tools):
    assert rank("what is it", tools) == tools                      # nothing left: everything
    assert not rank("xylophone", tools)


async def test_native_tools_say_whether_they_change_anything():
    read_only = {"mail": {"list_accounts", "list_mailboxes", "list_emails", "search_emails", "get_email",
                          "get_thread"},
                 "calendar": {"list_accounts", "list_calendars", "list_events", "search_events", "get_event"}}
    destructive = {"mail": {"move_email"}, "calendar": {"update_event", "delete_event"}}
    for name, connector in (("mail", mail_connector), ("calendar", calendar_connector)):
        server = connector.build([], ConnectorContext("t", name, logging.getLogger("t")))
        listed = {t.name: t.annotations for t in await server.list_tools()}
        assert all(a is not None and a.open_world_hint is False for a in listed.values())
        assert {n for n, a in listed.items() if a.read_only_hint} == read_only[name]
        assert {n for n, a in listed.items() if a.destructive_hint} == destructive[name]
