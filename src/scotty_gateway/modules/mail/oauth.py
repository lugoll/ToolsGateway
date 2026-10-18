"""`scotty-gateway auth <server> <account>`: authorise a Gmail account once.

Installed-app OAuth flow with a loopback redirect. The container listens on
port 8765 (publish it: `docker compose run --rm -p 127.0.0.1:8765:8765 ...`;
on the Pi add `ssh -L 8765:localhost:8765 <pi>` from the machine with the
browser). The browser must be signed in to the account being authorised.

Before the token is saved, the script checks that Google granted exactly
gmail.modify and that the signed-in mailbox is the configured address.
Client id and secret come from GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET
(a "Desktop app" client of a Google Cloud project whose consent screen is
published "In production": in "Testing", refresh tokens expire after 7 days).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .. import ModuleContext
from .config import parse_config
from .gmail_backend import API, SCOPE


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".token-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def auth(config: dict, ctx: ModuleContext, argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog=f"scotty-gateway auth {ctx.server}")
    p.add_argument("account")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args(argv)

    cfg = parse_config(config, ctx.data_dir)
    acc = cfg.accounts.get(args.account)
    if acc is None or acc.backend != "gmail":
        gmail = [n for n, a in cfg.accounts.items() if a.backend == "gmail"]
        sys.exit(f"'{args.account}' is not a Gmail account of {ctx.server} (Gmail: {', '.join(gmail) or 'none'})")
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET must be set (.env)")

    from google.auth.transport.requests import AuthorizedSession
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config({"installed": {
        "client_id": client_id, "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"]}}, scopes=[SCOPE])
    print(f"Authorising {acc.email} ({ctx.server}/{acc.name}) for {SCOPE}.\n"
          f"Sign in as {acc.email}. Google warns that the app is unverified: "
          "Advanced -> continue.\n", flush=True)
    creds = flow.run_local_server(
        host="localhost", bind_addr="0.0.0.0", port=args.port, open_browser=False,
        authorization_prompt_message="Open this URL in the browser:\n\n{url}\n",
        success_message="Done. You can close this window.",
        login_hint=acc.email, access_type="offline", prompt="consent")

    granted = set(creds.granted_scopes or creds.scopes or [])
    if granted != {SCOPE}:
        sys.exit(f"refusing to save: Google granted {sorted(granted)}, expected exactly {SCOPE}")
    if not creds.refresh_token:
        sys.exit("refusing to save: no refresh token in the answer (revoke the app's access in "
                 "myaccount.google.com -> Security -> Third-party access, then retry)")
    profile = AuthorizedSession(creds).get(f"{API}/profile", timeout=20).json()
    signed_in = str(profile.get("emailAddress", "")).lower()
    if signed_in != acc.email.lower():
        sys.exit(f"refusing to save: signed in as {signed_in or '?'}, but the account is {acc.email}")

    _write_private(acc.token_file, creds.to_json())
    print(f"Saved {acc.token_file} (mode 0600). {acc.email} is connected; "
          f"{json.dumps(profile.get('messagesTotal'))} messages visible.")
    return 0
