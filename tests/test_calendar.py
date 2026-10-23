"""The calendar connector: CalDAV against a real Radicale server, Google against a fake session."""
import json
import threading
from datetime import date, datetime, timedelta, timezone
from wsgiref.simple_server import WSGIRequestHandler, make_server

import caldav
import pytest
from fastmcp.exceptions import ToolError

from conftest import free_port
from gateway.connectors import AccountRuntime, ConnectorContext
from gateway.connectors.calendar import build, tools
from gateway.connectors.calendar.config import CalAccount
from gateway.connectors.calendar.gcal import GoogleCalendarBackend
from gateway.connectors.calendar.model import CalendarError, Moment


class _Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def radicale_url(tmp_path_factory):
    import radicale
    import radicale.config
    config = radicale.config.load()
    config.update({"auth": {"type": "none", "delay": "0"}, "rights": {"type": "owner_only"},
                   "storage": {"filesystem_folder": str(tmp_path_factory.mktemp("radicale"))}}, "test")
    port = free_port()
    server = make_server("127.0.0.1", port, radicale.Application(config), handler_class=_Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/"
    server.shutdown()


@pytest.fixture
def dav_user(radicale_url, request):
    """A fresh CalDAV user with two calendars and one event of someone else's meeting with guests."""
    user = request.node.name[:40].replace("[", "-").replace("]", "")
    client = caldav.DAVClient(url=radicale_url, username=user, password="pw", require_tls=False)
    principal = client.principal()
    work = principal.make_calendar(name="Work", cal_id="work")
    principal.make_calendar(name="Home", cal_id="home")
    work.save_event("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:test\nBEGIN:VEVENT\nUID:guests-1\n"
                    "DTSTAMP:20261001T000000Z\nDTSTART:20261022T090000Z\nDTEND:20261022T100000Z\n"
                    "SUMMARY:Board meeting\nORGANIZER:mailto:boss@example.org\n"
                    "ATTENDEE:mailto:me@example.org\nEND:VEVENT\nEND:VCALENDAR\n")
    work.save_event("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:test\nBEGIN:VEVENT\nUID:weekly-1\n"
                    "DTSTAMP:20261001T000000Z\nDTSTART:20261020T070000Z\nDTEND:20261020T073000Z\n"
                    "RRULE:FREQ=WEEKLY;COUNT=4\nSUMMARY:Standup\nEND:VEVENT\nEND:VCALENDAR\n")
    return radicale_url, user


@pytest.fixture
def changes():
    return []


def server_for(accounts, changes):
    import logging
    ctx = ConnectorContext("t", "calendar", logging.getLogger("t"),
                           record_change=lambda acc, action, fields: changes.append((acc, action, fields)))
    return build(accounts, ctx)


def dav_account(url, user, caps=("read", "write")):
    return AccountRuntime(name="home", label="Home", backend="caldav",
                          values={"url": url, "username": user, "auth_type": "basic", "timezone": "Europe/Berlin",
                                  "default_calendar": "Home"},
                          secrets={"password": "pw"}, capabilities=set(caps))


async def call(server, tool, **args):
    return (await server.call_tool(tool, args)).structured_content


async def test_exact_tool_set():
    server = server_for([], [])
    assert {t.name for t in await server.list_tools()} == set(tools.TOOLS)
    for cls in (tools.CaldavBackend, GoogleCalendarBackend):     # nothing that invites
        assert not [m for m in dir(cls) if any(w in m.lower() for w in ("invite", "attendee", "send"))]


async def test_caldav_read_create_update(dav_user, changes):
    url, user = dav_user
    server = server_for([dav_account(url, user)], changes)
    cals = (await call(server, "list_calendars"))["calendars"]
    assert sorted(c["name"] for c in cals) == ["Home", "Work"]

    week = await call(server, "list_events", start="2026-10-19", end="2026-11-16")
    standups = [e for e in week["events"] if e["summary"] == "Standup"]
    assert len(standups) == 4 and all(e["recurring"] for e in standups)        # expanded occurrences
    board = next(e for e in week["events"] if e["summary"] == "Board meeting")
    assert board["has_guests"] and board["start"] == "2026-10-22T09:00:00+00:00"

    created = await call(server, "create_event", summary="Zahnarzt", start="2026-10-23T14:00",
                         location="Praxis", description="Kontrolle")
    assert created["calendar"] == "home" and created["start"] == "2026-10-23T14:00:00+02:00"
    assert created["end"] == "2026-10-23T15:00:00+02:00"                       # default one hour
    allday = await call(server, "create_event", summary="Urlaub", start="2026-10-26", end="2026-10-31",
                        calendar="Work")
    assert allday["all_day"] and allday["end"] == "2026-10-31"

    moved = await call(server, "update_event", id=created["id"], start="2026-10-23T16:30")
    assert moved["start"] == "2026-10-23T16:30:00+02:00" and moved["end"] == "2026-10-23T17:30:00+02:00"
    got = await call(server, "get_event", id=created["id"])
    assert got["location"] == "Praxis" and got["description"] == "Kontrolle" and got["start"] == moved["start"]

    found = await call(server, "search_events", query="zahnarzt", start="2026-10-01", end="2026-12-01")
    assert [e["summary"] for e in found["events"]] == ["Zahnarzt"]

    with pytest.raises(ToolError, match="has guests"):
        await call(server, "update_event", id=board["id"], summary="Hijacked")
    with pytest.raises(ToolError, match="recurring"):
        await call(server, "update_event", id=standups[0]["id"], summary="Moved")
    with pytest.raises(ToolError, match="does not allow delete"):           # delete is off by default
        await call(server, "delete_event", id=created["id"])
    with pytest.raises(ToolError, match="end after it starts"):
        await call(server, "create_event", summary="x", start="2026-10-23T14:00", end="2026-10-23T13:00")
    assert [a for _, a, _ in changes] == ["create_event", "create_event", "update_event"]
    assert changes[0][2]["summary"] == "Zahnarzt"


async def test_caldav_without_uid_search(dav_user, changes, monkeypatch):
    """iCloud answers a UID search with 412: events are found at <uid>.ics instead."""
    def refuse(self, uid):
        raise caldav.lib.error.ReportError("412 Precondition Failed")
    monkeypatch.setattr(caldav.Calendar, "event_by_uid", refuse)
    url, user = dav_user
    server = server_for([dav_account(url, user, ("read", "write", "delete"))], changes)
    created = await call(server, "create_event", summary="Friseur", start="2026-10-27T09:00")
    assert created["id"].endswith("@tools-gateway")                          # an @ in the href, quoted
    moved = await call(server, "update_event", id=created["id"], start="2026-10-27T10:00")
    assert moved["start"] == "2026-10-27T10:00:00+01:00"
    assert (await call(server, "get_event", id=created["id"]))["start"] == moved["start"]
    with pytest.raises(ToolError, match="has guests"):                      # found, still guarded
        await call(server, "update_event", id="work|guests-1", summary="Hijacked")
    await call(server, "delete_event", id=created["id"])
    with pytest.raises(ToolError, match="not found"):
        await call(server, "get_event", id=created["id"])


async def test_caldav_delete_when_allowed(dav_user, changes):
    url, user = dav_user
    server = server_for([dav_account(url, user, ("read", "write", "delete"))], changes)
    created = await call(server, "create_event", summary="Temp", start="2026-10-24")
    out = await call(server, "delete_event", id=created["id"])
    assert out["deleted"] and out["summary"] == "Temp"
    with pytest.raises(ToolError, match="not found"):
        await call(server, "get_event", id=created["id"])
    board = next(e for e in (await call(server, "list_events", start="2026-10-22", end="2026-10-23"))["events"]
                 if e["summary"] == "Board meeting")
    with pytest.raises(ToolError, match="has guests"):
        await call(server, "delete_event", id=board["id"])


async def test_caldav_wrong_password_is_one_failed_account(dav_user, changes, monkeypatch):
    url, user = dav_user
    good = dav_account(url, user)
    broken = AccountRuntime(name="nopw", label="x", backend="caldav", values={"url": url, "username": user},
                            secrets={}, capabilities={"read"})
    unreachable = AccountRuntime(name="away", label="x", backend="caldav",
                                 values={"url": "http://127.0.0.1:9/", "username": "u"},
                                 secrets={"password": "p"}, capabilities={"read"})
    bad_zone = AccountRuntime(name="mars", label="x", backend="caldav",
                              values={"url": url, "username": user, "timezone": "Mars/Olympus"},
                              secrets={"password": "p"}, capabilities={"read"})
    server = server_for([good, broken, unreachable, bad_zone], changes)
    out = await call(server, "list_events", start="2026-10-19", end="2026-10-26")
    assert {e["account"] for e in out["events"]} == {"home"} and "cannot reach" in out["errors"]["away"]
    # a wrong argument fails every account, so the call fails instead of reading as "nothing on"
    with pytest.raises(ToolError, match="home: start: expected YYYY-MM-DD"):
        await call(server, "list_events", start="tomorrow")
    with pytest.raises(ToolError, match="end must be after start"):
        await call(server, "search_events", query="x", start="2026-10-30", end="2026-10-20")
    with pytest.raises(ToolError, match="cannot reach"):                        # the only account named
        await call(server, "list_events", account="away")
    accounts = {a["account"]: a for a in (await call(server, "list_accounts"))["accounts"]}
    assert accounts["nopw"]["status"] == "not usable: account nopw: no password set"
    assert "unknown time zone 'Mars/Olympus'" in accounts["mars"]["status"]


# ------------------------------------------------------------------ Google
class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode() if body is not None else b""
        self.text = self.content.decode()

    def json(self):
        return self._body


class FakeGoogle:
    def __init__(self):
        self.calls = []
        self.event = {"id": "e1", "summary": "Lunch", "start": {"dateTime": "2026-10-21T12:00:00+02:00"},
                      "end": {"dateTime": "2026-10-21T13:00:00+02:00"}, "status": "confirmed"}

    def request(self, method, url, timeout=None, params=None, json=None):
        path = url.split("/calendar/v3", 1)[1]
        self.calls.append((method, path, params, json))
        if path == "/users/me/calendarList":
            return Resp(200, {"items": [{"id": "me@gmail.com", "summary": "Me", "primary": True, "accessRole": "owner"},
                                        {"id": "family#x@group", "summary": "Family", "accessRole": "reader"}]})
        if path.endswith("/events") and method == "GET":
            return Resp(200, {"items": [self.event]})
        if path.endswith("/events") and method == "POST":
            return Resp(200, {"id": "new1", **json})
        if path.endswith("/events/e1") and method == "GET":
            return Resp(200, self.event)
        if path.endswith("/events/e1") and method == "PATCH":
            return Resp(200, {**self.event, **json})
        if path.endswith("/events/e1") and method == "DELETE":
            return Resp(204, None)
        if path.endswith("/events/gone"):
            return Resp(404, {"error": {"message": "Not Found"}})
        raise AssertionError(path)


@pytest.fixture
def google():
    acc = CalAccount(name="g", backend="google", label="Google", capabilities={"read", "write", "delete"},
                     timezone="Europe/Berlin", token="{}")
    b = GoogleCalendarBackend(acc, 10)
    b._session = FakeGoogle()
    return b


def test_google_reads_and_never_notifies(google):
    lo = datetime(2026, 10, 21, tzinfo=timezone.utc)
    events = google.events(lo, lo + timedelta(days=1), None)
    assert len(events) == 2 and events[0]["id"] == "me@gmail.com|e1"       # one per calendar
    list_call = google._session.calls[1]
    assert list_call[2]["singleEvents"] == "true" and list_call[2]["timeMin"] == lo.isoformat()
    created = google.create(None, "Dentist", Moment(date(2026, 10, 24)), Moment(date(2026, 10, 25)), None, None)
    method, path, params, body = google._session.calls[-1]
    assert path == "/calendars/primary/events" and params == {"sendUpdates": "none"}
    assert body == {"summary": "Dentist", "start": {"date": "2026-10-24"}, "end": {"date": "2026-10-25"}}
    assert created["all_day"] and "attendees" not in body
    updated = google.update("me@gmail.com|e1", {"start": Moment(datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc))})
    assert google._session.calls[-1][2] == {"sendUpdates": "none"}
    assert updated["end"] == "2026-10-21T15:00:00+00:00"                     # duration kept
    google.delete("me@gmail.com|e1")
    assert google._session.calls[-1][:3] == ("DELETE", "/calendars/me%40gmail.com/events/e1", {"sendUpdates": "none"})


def test_google_refuses_guests_and_recurring(google):
    google._session.event = {**google._session.event, "attendees": [{"email": "x@y.org"}]}
    with pytest.raises(CalendarError, match="has guests"):
        google.delete("primary|e1")
    google._session.event = {**google._session.event, "attendees": [], "recurringEventId": "r1"}
    with pytest.raises(CalendarError, match="recurring"):
        google.update("primary|e1", {"summary": "x"})
    with pytest.raises(CalendarError, match="not found"):
        google.get("primary|gone")
    with pytest.raises(CalendarError, match="not an event id"):
        google.get("nonsense")
