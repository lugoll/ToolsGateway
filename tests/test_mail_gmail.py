"""Gmail backend request logic against a fake AuthorizedSession."""
import base64
import json

import pytest

from scotty_gateway.modules.mail.config import Account, MailError
from scotty_gateway.modules.mail.gmail_backend import GmailBackend, NeedsAuth
from scotty_gateway.modules.mail.parse import build_draft
from test_mail_parse import make


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


class FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, timeout=None, params=None, json=None):
        path = url.split("/users/me", 1)[1]
        self.calls.append((method, path, params, json))
        if path == "/labels":
            return Resp(200, {"labels": [{"id": "INBOX", "name": "INBOX"}, {"id": "Label_1", "name": "Rechnungen"}]})
        if path == "/messages" and method == "GET":
            return Resp(200, {"messages": [{"id": "m1"}], "resultSizeEstimate": 1})
        if path == "/messages/m1" and dict(params if isinstance(params, dict) else params or []).get("format") == "raw":
            raw = base64.urlsafe_b64encode(make()).decode().rstrip("=")
            return Resp(200, {"id": "m1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD"], "raw": raw})
        if path == "/messages/m1" and dict(params if isinstance(params, dict) else params or []).get("format") == "minimal":
            return Resp(200, {"id": "m1", "threadId": "t1"})
        if path == "/messages/m1":
            return Resp(200, {"id": "m1", "threadId": "t1", "labelIds": ["UNREAD"], "internalDate": "1792224000000",
                              "snippet": "Hallo", "payload": {"headers": [
                                  {"name": "From", "value": "A <a@x.org>"}, {"name": "Subject", "value": "Hi"}]}})
        if path == "/messages/m1/modify" or path == "/drafts":
            return Resp(200, {"id": "d1"})
        if path == "/messages/gone":
            return Resp(404, {"error": {"message": "Requested entity was not found."}})
        if path == "/messages/expired":
            return Resp(401, {})
        raise AssertionError(path)


@pytest.fixture
def gmail(tmp_path):
    acc = Account(name="gmail", backend="gmail", label="Gmail", email="me@gmail.com",
                  token_file=tmp_path / "gmail.json")
    b = GmailBackend(acc, 10, 1000, "email-t")
    b._session = FakeSession()
    return b


def test_list_unread_in_label(gmail):
    out = gmail.list("Rechnungen", False, None, None, None, 10, 1)
    method, path, params, _ = gmail._session.calls[1]
    assert ("q", "is:unread") in params and ("labelIds", "Label_1") in params and ("maxResults", 10) in params
    assert out["emails"][0] == {"id": "m1", "thread_id": "t1", "from": "A <a@x.org>", "to": "", "subject": "Hi",
                                "date": "2026-10-17T08:00:00+00:00", "unread": True, "snippet": "Hallo"}


def test_get_parses_raw(gmail):
    out = gmail.get("m1")
    assert out["subject"] == "Grüße" and out["unread"] and out["thread_id"] == "t1"


def test_mark_and_draft(gmail):
    gmail.mark("m1", True)
    assert gmail._session.calls[-1][3] == {"removeLabelIds": ["UNREAD"]}
    msg = build_draft("me@gmail.com", ["a@x.org"], [], "Re", "body", None)
    assert gmail.save_draft(msg, "m1") == {"folder": "Drafts", "draft_id": "d1"}
    assert gmail._session.calls[-1][3]["message"]["threadId"] == "t1"


def test_errors(gmail, tmp_path):
    with pytest.raises(MailError, match="not found"):
        gmail.get("gone")
    with pytest.raises(NeedsAuth, match="scotty-gateway auth email-t gmail"):
        gmail.get("expired")
    fresh = GmailBackend(gmail.acc, 10, 1000, "email-t")
    with pytest.raises(NeedsAuth):
        fresh.mailboxes()          # no token file yet


def test_unknown_label(gmail):
    with pytest.raises(MailError, match="not found"):
        gmail.list("Nope", None, None, None, None, 10, 1)
