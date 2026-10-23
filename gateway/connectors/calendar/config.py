"""Calendar accounts from connections (catalog entries icloud-calendar, google-calendar, caldav-calendar).

Values per account (catalog settings + the user's fields):
    caldav:  url (server or principal URL), username, timezone, default_calendar,
             auth_type (basic | digest; unset = ask the server first, one round trip more)
             secret password
    google:  email, timezone, default_calendar; secret oauth_token (from the Google flow)

capabilities: `read` lists and opens events; `write` creates and changes
events; `delete` removes events (off by default). Guests can never be added,
so no tool makes the provider send an invitation.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import AccountRuntime
from .model import CalendarError, zone

CAPABILITIES = ("read", "write", "delete")
BACKENDS = {"caldav", "google"}


class CalendarConfigError(ValueError):
    pass


@dataclass
class CalAccount:
    name: str
    backend: str
    label: str
    capabilities: set[str]
    timezone: str = "Europe/Berlin"
    default_calendar: str | None = None
    url: str | None = None
    auth_type: str | None = None
    username: str | None = None
    email: str | None = None
    password: str | None = field(default=None, repr=False)
    token: str | None = field(default=None, repr=False)


def account_from_runtime(rt: AccountRuntime) -> CalAccount:
    v = rt.values
    where = f"account {rt.name}"
    if rt.backend not in BACKENDS:
        raise CalendarConfigError(f"{where}: backend one of {', '.join(sorted(BACKENDS))}")
    caps = set(rt.capabilities)
    if caps - set(CAPABILITIES):
        raise CalendarConfigError(f"{where}: capabilities only {', '.join(CAPABILITIES)}")
    acc = CalAccount(name=rt.name, backend=rt.backend, label=rt.label or rt.name, capabilities=caps,
                     timezone=str(v.get("timezone") or "Europe/Berlin"),
                     default_calendar=v.get("default_calendar") or None)
    try:
        zone(acc.timezone)
    except CalendarError as exc:
        raise CalendarConfigError(f"{where}: {exc}") from None
    if rt.backend == "caldav":
        acc.url = v.get("url")
        acc.auth_type = v.get("auth_type") or None
        acc.username = v.get("username") or v.get("email")
        acc.password = rt.secrets.get("password")
        if not acc.url or not acc.username:
            raise CalendarConfigError(f"{where}: url and username are required")
        if not acc.password:
            raise CalendarConfigError(f"{where}: no password set")
    else:
        acc.email = v.get("email")
        acc.token = rt.secrets.get("oauth_token")
    return acc
