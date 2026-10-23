"""Google calendars through the Calendar API v3 with OAuth.

Scopes: calendar.events (read and change events) and
calendar.calendarlist.readonly (see which calendars exist); nothing that
changes sharing or settings. Every write passes sendUpdates=none, events with
guests are not changed or deleted, and guests cannot be added: no tool makes
Google email anyone. Recurring events are expanded (singleEvents) for reading
and are read-only, as on CalDAV accounts.

Event ids are "<calendarId>|<eventId>".
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from urllib.parse import quote

from .config import CalAccount
from .model import CalendarError, Moment, check_span, clip, iso, zone

SCOPES = ["https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/calendar.calendarlist.readonly"]
API = "https://www.googleapis.com/calendar/v3"


class NeedsAuth(CalendarError):
    pass


class GoogleCalendarBackend:
    def __init__(self, account: CalAccount, timeout: float,
                 auth_hint: Callable[[str], str] = lambda account: "connect it in the web UI"):
        self.acc = account
        self.timeout = timeout
        self.tz = zone(account.timezone)
        self.hint = auth_hint
        self._session = None

    # -------------------------------------------------------------- plumbing
    def _auth_hint(self) -> str:
        return f"Google calendar account {self.acc.name} needs authorisation: {self.hint(self.acc.name)}"

    def token_present(self) -> bool:
        return bool(self.acc.token)

    def session(self):
        if self._session is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2.credentials import Credentials
            if not self.acc.token:
                raise NeedsAuth(self._auth_hint())
            try:
                creds = Credentials.from_authorized_user_info(json.loads(self.acc.token), SCOPES)
            except ValueError as exc:
                raise NeedsAuth(f"{self._auth_hint()} ({exc})") from None
            self._session = AuthorizedSession(creds)
        return self._session

    def _call(self, method: str, path: str, **kwargs) -> dict:
        from google.auth.exceptions import RefreshError
        try:
            resp = self.session().request(method, f"{API}{path}", timeout=self.timeout, **kwargs)
        except RefreshError as exc:
            self._session = None
            raise NeedsAuth(f"{self._auth_hint()} ({exc})") from None
        except OSError as exc:
            raise CalendarError(f"Google Calendar unreachable: {exc}") from None
        if resp.status_code == 401:
            self._session = None
            raise NeedsAuth(self._auth_hint())
        if resp.status_code == 404:
            raise CalendarError("not found (check the calendar or event id)")
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error", {}).get("message", resp.text)
            except ValueError:
                detail = resp.text
            raise CalendarError(f"Google Calendar {resp.status_code}: {clip(str(detail), 300)}")
        return resp.json() if resp.content else {}

    def _split(self, event_id: str) -> tuple[str, str]:
        cal, sep, eid = str(event_id).rpartition("|")
        if not sep or not cal or not eid:
            raise CalendarError(f"'{event_id}' is not an event id of this account; use the id from list_events")
        return cal, eid

    def _calendar_id(self, calendar: str | None, for_write: bool = False) -> str:
        wanted = calendar or (self.acc.default_calendar if for_write else None) or "primary"
        if wanted == "primary":
            return wanted
        for c in self.calendars():
            if wanted in (c["id"], c["name"]):
                return c["id"]
        raise CalendarError(f"calendar '{wanted}' not found; see list_calendars")

    @staticmethod
    def _when(m: Moment) -> dict:
        return {"date": m.value.isoformat()} if m.all_day else {"dateTime": m.value.isoformat()}

    def _parse_when(self, raw: dict) -> date | datetime:
        if "date" in raw:
            return date.fromisoformat(raw["date"])
        dt = datetime.fromisoformat(raw["dateTime"].replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=self.tz)

    def _to_event(self, cal_id: str, e: dict) -> dict:
        start = self._parse_when(e.get("start", {}))
        end = self._parse_when(e["end"]) if e.get("end") else start
        return {
            "id": f"{cal_id}|{e['id']}", "calendar": cal_id, "summary": e.get("summary") or "(no title)",
            "start": iso(start), "end": iso(end), "all_day": "date" in e.get("start", {}),
            "location": e.get("location", ""), "description": clip(e.get("description")),
            "recurring": bool(e.get("recurringEventId") or e.get("recurrence")),
            "has_guests": bool(e.get("attendees")), "status": e.get("status", "confirmed"),
        }

    def _list(self, cal_id: str, params: dict) -> list[dict]:
        out, token = [], None
        for _ in range(10):                      # at most 2500 events per call
            page = self._call("GET", f"/calendars/{quote(cal_id, safe='')}/events",
                              params={**params, "singleEvents": "true", "orderBy": "startTime", "maxResults": 250,
                                      **({"pageToken": token} if token else {})})
            out += [self._to_event(cal_id, e) for e in page.get("items", []) if e.get("status") != "cancelled"]
            token = page.get("nextPageToken")
            if not token:
                break
        return out

    # ---------------------------------------------------------------- reads
    def calendars(self) -> list[dict]:
        items = self._call("GET", "/users/me/calendarList").get("items", [])
        return [{"id": c["id"], "name": c.get("summaryOverride") or c.get("summary", c["id"]),
                 "primary": bool(c.get("primary")), "writable": c.get("accessRole") in ("owner", "writer")}
                for c in items]

    def events(self, start: datetime, end: datetime, calendar: str | None) -> list[dict]:
        cal_ids = [self._calendar_id(calendar)] if calendar else [c["id"] for c in self.calendars()]
        out = []
        for cal_id in cal_ids:
            out += self._list(cal_id, {"timeMin": start.isoformat(), "timeMax": end.isoformat()})
        return out

    def get(self, event_id: str) -> dict:
        cal, eid = self._split(event_id)
        return self._to_event(cal, self._call("GET", f"/calendars/{quote(cal, safe='')}/events/{quote(eid, safe='')}"))

    # --------------------------------------------------------------- writes
    def create(self, calendar: str | None, summary: str, start: Moment, end: Moment,
               location: str | None, description: str | None) -> dict:
        cal = self._calendar_id(calendar, for_write=True)
        body = {"summary": summary, "start": self._when(start), "end": self._when(end)}
        if location:
            body["location"] = location
        if description:
            body["description"] = description
        created = self._call("POST", f"/calendars/{quote(cal, safe='')}/events", params={"sendUpdates": "none"},
                             json=body)
        return self._to_event(cal, created)

    def _editable(self, cal: str, eid: str, what: str) -> dict:
        e = self._call("GET", f"/calendars/{quote(cal, safe='')}/events/{quote(eid, safe='')}")
        if e.get("recurringEventId") or e.get("recurrence"):
            raise CalendarError(f"'{e.get('summary')}' is a recurring event; {what} it in the calendar app")
        if e.get("attendees"):
            raise CalendarError(f"'{e.get('summary')}' has guests, who would be notified; {what} it in the calendar app")
        return e

    def update(self, event_id: str, changes: dict) -> dict:
        cal, eid = self._split(event_id)
        e = self._editable(cal, eid, "change")
        body = {k: changes[k] or "" for k in ("summary", "location", "description") if k in changes}
        if "start" in changes or "end" in changes:
            old_start = self._parse_when(e["start"])
            old_end = self._parse_when(e["end"])
            start = changes.get("start") or Moment(old_start)
            end = changes.get("end") or Moment(start.value + (old_end - old_start))
            check_span(start, end)
            body["start"], body["end"] = self._when(start), self._when(end)
        updated = self._call("PATCH", f"/calendars/{quote(cal, safe='')}/events/{quote(eid, safe='')}",
                             params={"sendUpdates": "none"}, json=body)
        return self._to_event(cal, updated)

    def delete(self, event_id: str) -> dict:
        cal, eid = self._split(event_id)
        e = self._editable(cal, eid, "delete")
        self._call("DELETE", f"/calendars/{quote(cal, safe='')}/events/{quote(eid, safe='')}",
                   params={"sendUpdates": "none"})
        return self._to_event(cal, e)

