"""The provider-neutral event shape and time parsing shared by the calendar backends.

An event as the agent sees it:
    {"id", "calendar", "summary", "start", "end", "all_day", "location",
     "description", "recurring", "has_guests", "status"}
start/end are ISO 8601: a date ("2026-10-21") for all-day events (end is
exclusive, as in iCalendar), else a datetime with UTC offset.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DESCRIPTION_MAX = 2000


class CalendarError(RuntimeError):
    """A failure worth showing to the agent as is (no secrets in the text)."""


@dataclass
class Moment:
    """A parsed tool argument: a date (all-day) or an aware datetime."""
    value: date | datetime

    @property
    def all_day(self) -> bool:
        return not isinstance(self.value, datetime)


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        raise CalendarError(f"unknown time zone '{name}'") from None


def parse_moment(text: str, tz: ZoneInfo, what: str = "time") -> Moment:
    """'2026-10-21' (all day) or '2026-10-21T14:00' (in the account's zone unless it has an offset)."""
    text = str(text).strip()
    try:
        if len(text) == 10:
            return Moment(date.fromisoformat(text))
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise CalendarError(f"{what}: expected YYYY-MM-DD or YYYY-MM-DDTHH:MM, got '{text}'") from None
    return Moment(dt if dt.tzinfo else dt.replace(tzinfo=tz))


def window(start: str | None, end: str | None, tz: ZoneInfo, default_days: int = 7) -> tuple[datetime, datetime]:
    """A search range as aware datetimes; dates mean whole days in the account's zone."""
    def as_dt(m: Moment, end_of_day: bool) -> datetime:
        if m.all_day:
            d = m.value + timedelta(days=1) if end_of_day else m.value
            return datetime.combine(d, time(0), tz)
        return m.value
    now = datetime.now(tz)
    lo = as_dt(parse_moment(start, tz, "start"), False) if start else datetime.combine(now.date(), time(0), tz)
    hi = as_dt(parse_moment(end, tz, "end"), True) if end else lo + timedelta(days=default_days)
    if hi <= lo:
        raise CalendarError("end must be after start")
    if hi - lo > timedelta(days=366):
        raise CalendarError("search at most one year at a time")
    return lo, hi


def iso(value: date | datetime) -> str:
    return value.isoformat()


def check_span(start: Moment, end: Moment) -> None:
    if start.all_day != end.all_day:
        raise CalendarError("start and end must both be dates (all day) or both be times")
    if end.value <= start.value:
        raise CalendarError("the event must end after it starts")


def default_end(start: Moment) -> Moment:
    return Moment(start.value + (timedelta(days=1) if start.all_day else timedelta(hours=1)))


def clip(text: str | None, limit: int = DESCRIPTION_MAX) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def sort_key(event: dict) -> datetime:
    """Chronological across zones, all-day events first on their day (as midnight UTC)."""
    start = event.get("start") or "1970-01-01"
    if "T" not in start:
        return datetime.combine(date.fromisoformat(start), time(0), timezone.utc)
    dt = datetime.fromisoformat(start)
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
