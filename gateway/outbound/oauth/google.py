"""Connecting a Gmail or Google Calendar account: OAuth with the instance's Google client.

Google accepts redirect URIs only on public domains or loopback addresses, and
the gateway lives at a private name (tools.pi.home). So the flow uses a
"Desktop app" client with the loopback redirect http://localhost and a
paste-back step:

  1. the UI sends the user to Google's consent page (PKCE, login_hint, offline access)
  2. Google redirects the browser to http://localhost/?code=...; that page does
     not load, which is expected
  3. the user pastes that address into the UI; the gateway exchanges the code

Before the token is saved the gateway checks that Google granted exactly the
catalog's scopes, returned a refresh token, and that the signed-in account is
the configured address. Stored per connection (encrypted secret `oauth_token`):
{"refresh_token", "scopes", "email"}. The client id and secret are combined
with it when the connection is built (authorized_user_info), so rotating the
client secret does not invalidate the accounts.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT_URI = "http://localhost"
PENDING_TTL = 15 * 60
TIMEOUT = 20

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"


class GoogleAuthError(RuntimeError):
    """Shown to the user as is."""


@dataclass
class Pending:
    user_id: int
    connection_id: int
    email: str
    scopes: list[str]
    verifier: str
    created: float = field(default_factory=time.monotonic)


def authorized_user_info(stored: str, client_id: str, client_secret: str) -> str:
    """The JSON google-auth's Credentials.from_authorized_user_info expects."""
    data = json.loads(stored)
    return json.dumps({"type": "authorized_user", "client_id": client_id, "client_secret": client_secret,
                       "refresh_token": data["refresh_token"], "scopes": data.get("scopes"),
                       "token_uri": TOKEN_URL})


class GoogleOAuth:
    def __init__(self, secrets_store):
        self.secrets = secrets_store
        self._pending: dict[str, Pending] = {}

    def client(self) -> tuple[str, str]:
        cid = self.secrets.get_instance("google_client_id")
        secret = self.secrets.get_instance("google_client_secret")
        if not cid or not secret:
            raise GoogleAuthError("the admin has not set up the Google OAuth client yet (Settings)")
        return cid, secret

    def _expire(self) -> None:
        now = time.monotonic()
        self._pending = {k: p for k, p in self._pending.items() if now - p.created < PENDING_TTL}

    def start(self, user_id: int, connection_id: int, email: str, scopes: list[str]) -> str:
        """The consent URL for this connection."""
        client_id, _ = self.client()
        self._expire()
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self._pending[state] = Pending(user_id, connection_id, email, list(scopes), verifier)
        params = {"client_id": client_id, "redirect_uri": REDIRECT_URI, "response_type": "code",
                  "scope": " ".join(scopes), "access_type": "offline", "prompt": "consent",
                  "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
        if email:
            params["login_hint"] = email
        return f"{AUTH_URL}?{urlencode(params)}"

    def finish(self, user_id: int, pasted: str) -> tuple[Pending, str]:
        """Exchange the code from the pasted redirect address; returns (pending, stored token JSON)."""
        query = parse_qs(urlsplit(pasted.strip()).query)
        state = (query.get("state") or [""])[0]
        if query.get("error"):
            raise GoogleAuthError(f"Google refused: {query['error'][0]}")
        self._expire()
        pending = self._pending.get(state)
        if pending is None or pending.user_id != user_id:
            raise GoogleAuthError("this address does not belong to a running connection attempt; start again")
        code = (query.get("code") or [""])[0]
        if not code:
            raise GoogleAuthError("no code in the address; paste the whole address from the browser's address bar")
        del self._pending[state]
        client_id, client_secret = self.client()
        try:
            resp = requests.post(TOKEN_URL, timeout=TIMEOUT, data={
                "code": code, "client_id": client_id, "client_secret": client_secret,
                "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code", "code_verifier": pending.verifier})
        except requests.RequestException as exc:
            raise GoogleAuthError(f"Google unreachable: {exc}") from None
        body = resp.json() if resp.content else {}
        if resp.status_code != 200:
            raise GoogleAuthError(f"Google refused the code: {body.get('error_description') or body.get('error')}")
        granted = set(body.get("scope", "").split())
        if granted != set(pending.scopes):
            raise GoogleAuthError(f"refusing to save: Google granted {sorted(granted)}, expected exactly "
                                  f"{sorted(pending.scopes)} (tick every box on the consent page)")
        if not body.get("refresh_token"):
            raise GoogleAuthError("refusing to save: no refresh token (remove the app's access in "
                                  "myaccount.google.com → Security → Third-party access, then retry)")
        signed_in = self._account_of(body["access_token"], granted)
        if not signed_in:
            raise GoogleAuthError("refusing to save: could not check which account signed in; try again")
        if pending.email and signed_in.lower() != pending.email.lower():
            raise GoogleAuthError(f"refusing to save: signed in as {signed_in}, but the connection is "
                                  f"for {pending.email}")
        return pending, json.dumps({"refresh_token": body["refresh_token"], "scopes": sorted(granted),
                                    "email": signed_in or pending.email})

    @staticmethod
    def _account_of(access_token: str, scopes: set[str]) -> str | None:
        """Which mailbox / calendar owner the token belongs to (from the API the scope opens)."""
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            if GMAIL_SCOPE in scopes:
                r = requests.get("https://gmail.googleapis.com/gmail/v1/users/me/profile", headers=headers,
                                 timeout=TIMEOUT)
                return r.json().get("emailAddress") if r.ok else None
            if CALENDAR_SCOPE in scopes:
                r = requests.get("https://www.googleapis.com/calendar/v3/calendars/primary", headers=headers,
                                 timeout=TIMEOUT)
                return r.json().get("id") if r.ok else None
        except requests.RequestException:
            return None
        return None
