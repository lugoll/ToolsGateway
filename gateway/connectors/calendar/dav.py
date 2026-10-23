"""CalDAV calendars (iCloud, Nextcloud, any CalDAV server) with an app password.

One client per call: CalDAV is plain HTTP with the password on every request,
so there is no login session to keep. Event ids are "<calendar>|<uid>" plus "|<recurrence-id>" for
one occurrence of a recurring event; <calendar> is the last segment of the
calendar's URL.

Never sends: there is no way to add guests, and events that have guests
(ATTENDEE) are not changed or deleted, because CalDAV servers such as iCloud
email invitations and cancellations on their own when such an event changes.
Recurring events are read-only for the same reason of blast radius.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlsplit

import caldav
import niquests
from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent

from .config import CalAccount
from .model import CalendarError, Moment, check_span, clip, iso, zone


def _key(cal) -> str:
    path = urlsplit(str(cal.url)).path.rstrip("/")
    return unquote(path.rsplit("/", 1)[-1]) or "calendar"


class CaldavBackend:
    def __init__(self, account: CalAccount, timeout: float):
        self.acc = account
        self.timeout = timeout
        self.tz = zone(account.timezone)

    # -------------------------------------------------------------- plumbing
    @contextmanager
    def _principal(self):
        try:
            client = caldav.DAVClient(url=self.acc.url, username=self.acc.username, password=self.acc.password,
                                      auth_type=self.acc.auth_type, timeout=int(self.timeout),
                                      require_tls=self.acc.url.startswith("https://"))
        except Exception as exc:  # noqa: BLE001
            raise CalendarError(f"cannot use {self.acc.url}: {exc}") from None
        # niquests upgrades to HTTP/3 when iCloud advertises it; QUIC from a
        # container then fails now and then with EMSGSIZE. TCP is enough.
        client.session.close()
        client.session = niquests.Session(disable_http3=True)
        try:
            try:
                principal = client.principal()
            except caldav.lib.error.AuthorizationError:
                raise CalendarError(f"login to {self.acc.url} failed; check the app password") from None
            except CalendarError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise CalendarError(f"cannot reach {self.acc.url}: {type(exc).__name__}: {exc}") from None
            yield principal
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _event_calendars(principal) -> list:
        out = []
        for cal in principal.calendars():
            try:
                comps = cal.get_supported_components()
            except Exception:  # noqa: BLE001 - servers that do not say: assume events
                comps = ["VEVENT"]
            if "VEVENT" in comps:
                out.append(cal)
        return out

    def _pick(self, principal, calendar: str | None, for_write: bool = False):
        cals = self._event_calendars(principal)
        if not cals:
            raise CalendarError("this account has no event calendars")
        wanted = calendar or (self.acc.default_calendar if for_write else None)
        if wanted is None:
            return cals[0]
        for cal in cals:
            if wanted in (_key(cal), cal.get_display_name()):
                return cal
        names = ", ".join(f"{c.get_display_name()} ({_key(c)})" for c in cals)
        raise CalendarError(f"calendar '{wanted}' not found; calendars: {names}")

    def _split(self, event_id: str) -> tuple[str, str, str | None]:
        parts = str(event_id).split("|")
        if len(parts) not in (2, 3) or not all(parts[:2]):
            raise CalendarError(f"'{event_id}' is not an event id of this account; use the id from list_events")
        return parts[0], parts[1], parts[2] if len(parts) == 3 else None

    def _moment(self, value) -> date | datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=self.tz)
        return value

    def _to_event(self, cal_key: str, comp) -> dict:
        start = self._moment(comp.get("dtstart").dt)
        if comp.get("dtend") is not None:
            end = self._moment(comp.get("dtend").dt)
        elif comp.get("duration") is not None:
            end = start + comp.get("duration").dt
        else:
            end = start + timedelta(days=1) if not isinstance(start, datetime) else start
        rid = comp.get("recurrence-id")
        event_id = f"{cal_key}|{comp.get('uid')}"
        if rid is not None:
            event_id += f"|{iso(self._moment(rid.dt))}"
        return {
            "id": event_id, "calendar": cal_key, "summary": str(comp.get("summary") or "(no title)"),
            "start": iso(start), "end": iso(end), "all_day": not isinstance(start, datetime),
            "location": str(comp.get("location") or ""), "description": clip(str(comp.get("description") or "")),
            "recurring": comp.get("rrule") is not None or rid is not None,
            "has_guests": bool(comp.get("attendee")), "status": str(comp.get("status") or "CONFIRMED").lower(),
        }

    @staticmethod
    def _master(obj, recurrence: str | None):
        comps = [c for c in obj.icalendar_instance.walk("VEVENT")]
        if recurrence:
            for c in comps:
                rid = c.get("recurrence-id")
                if rid is not None and iso(rid.dt) == recurrence:
                    return c
        return next((c for c in comps if c.get("recurrence-id") is None), comps[0] if comps else None)

    def _load(self, principal, event_id: str):
        """Most clients (Apple's, caldav's save_event) store an event at
        <calendar>/<uid>.ics: try that GET first. Otherwise search by UID, which
        iCloud refuses outright (412 on the REPORT)."""
        cal_key, uid, recurrence = self._split(event_id)
        cal = self._pick(principal, cal_key)
        try:
            obj = cal.event_by_url(cal.url.join(quote(uid.replace("/", "%2F")) + ".ics"))
            if str(obj.icalendar_component.get("uid")) == uid:
                return cal, obj, recurrence
        except caldav.lib.error.DAVError:
            pass
        try:
            obj = cal.event_by_uid(uid)
        except (caldav.lib.error.NotFoundError, caldav.lib.error.ReportError):
            raise CalendarError(f"event '{event_id}' not found") from None
        return cal, obj, recurrence

    # ---------------------------------------------------------------- reads
    def calendars(self) -> list[dict]:
        with self._principal() as p:
            return [{"id": _key(c), "name": c.get_display_name() or _key(c)} for c in self._event_calendars(p)]

    def events(self, start: datetime, end: datetime, calendar: str | None) -> list[dict]:
        with self._principal() as p:
            cals = [self._pick(p, calendar)] if calendar else self._event_calendars(p)
            out = []
            for cal in cals:
                for obj in cal.search(start=start, end=end, event=True, expand=True):
                    for comp in obj.icalendar_instance.walk("VEVENT"):
                        out.append(self._to_event(_key(cal), comp))
            return out

    def get(self, event_id: str) -> dict:
        with self._principal() as p:
            cal, obj, recurrence = self._load(p, event_id)
            comp = self._master(obj, recurrence)
            if comp is None:
                raise CalendarError(f"event '{event_id}' not found")
            event = self._to_event(_key(cal), comp)
            return {**event, "id": event_id}

    # --------------------------------------------------------------- writes
    def create(self, calendar: str | None, summary: str, start: Moment, end: Moment,
               location: str | None, description: str | None) -> dict:
        ev = IEvent()
        ev.add("uid", f"{uuid.uuid4()}@tools-gateway")
        ev.add("dtstamp", datetime.now(timezone.utc))
        ev.add("summary", summary)
        ev.add("dtstart", start.value)
        ev.add("dtend", end.value)
        if location:
            ev.add("location", location)
        if description:
            ev.add("description", description)
        ical = ICalendar()
        ical.add("prodid", "-//tools-gateway//calendar//EN")
        ical.add("version", "2.0")
        ical.add_component(ev)
        with self._principal() as p:
            cal = self._pick(p, calendar, for_write=True)
            cal.save_event(ical.to_ical().decode())
            return self._to_event(_key(cal), ev)

    def _editable(self, comp, event_id: str, what: str):
        if comp is None:
            raise CalendarError(f"event '{event_id}' not found")
        if comp.get("rrule") is not None or comp.get("recurrence-id") is not None:
            raise CalendarError(f"'{comp.get('summary')}' is a recurring event; {what} it in the calendar app")
        if comp.get("attendee"):
            raise CalendarError(f"'{comp.get('summary')}' has guests, who would be notified; "
                                f"{what} it in the calendar app")

    def update(self, event_id: str, changes: dict) -> dict:
        with self._principal() as p:
            cal, obj, recurrence = self._load(p, event_id)
            comp = self._master(obj, recurrence)
            self._editable(comp, event_id, "change")
            for key in ("summary", "location", "description"):
                if key in changes:
                    comp.pop(key, None)
                    if changes[key]:
                        comp.add(key, changes[key])
            if "start" in changes or "end" in changes:
                old_start = self._moment(comp.get("dtstart").dt)
                old_end = self._moment(self._end_of(comp))
                start = changes.get("start") or Moment(old_start)
                end = changes.get("end") or Moment(start.value + (old_end - old_start))   # keep the duration
                check_span(start, end)
                for key, m in (("dtstart", start), ("dtend", end)):
                    comp.pop(key, None)
                    comp.add(key, m.value)
                comp.pop("duration", None)
            comp.pop("dtstamp", None)
            comp.add("dtstamp", datetime.now(timezone.utc))
            obj.save()
            return self._to_event(_key(cal), comp)

    def _end_of(self, comp):
        if comp.get("dtend") is not None:
            return comp.get("dtend").dt
        if comp.get("duration") is not None:
            return comp.get("dtstart").dt + comp.get("duration").dt
        start = comp.get("dtstart").dt
        return start + timedelta(days=1) if not isinstance(start, datetime) else start

    def delete(self, event_id: str) -> dict:
        with self._principal() as p:
            cal, obj, recurrence = self._load(p, event_id)
            comp = self._master(obj, recurrence)
            self._editable(comp, event_id, "delete")
            event = self._to_event(_key(cal), comp)
            obj.delete()
            return event
