"""IMAP backend against a real server. Needs GreenMail:

    docker run -d --rm --name scotty-greenmail \
      -e GREENMAIL_OPTS='-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.auth.disabled' \
      -p 127.0.0.1:3025:3025 -p 127.0.0.1:3143:3143 greenmail/standalone:2.1.14
    SCOTTY_TEST_IMAP=127.0.0.1:3143 SCOTTY_TEST_SMTP=127.0.0.1:3025 uv run pytest -m imap
"""
import os
import smtplib
import uuid
from email.message import EmailMessage

import pytest
from imapclient import IMAPClient

from scotty_gateway.modules.mail.config import Account, MailError
from scotty_gateway.modules.mail.imap_backend import ImapBackend
from scotty_gateway.modules.mail.parse import build_draft

IMAP = os.environ.get("SCOTTY_TEST_IMAP")
SMTP = os.environ.get("SCOTTY_TEST_SMTP")
pytestmark = [pytest.mark.imap, pytest.mark.skipif(not (IMAP and SMTP), reason="SCOTTY_TEST_IMAP/SMTP unset")]


def deliver(user, subject, body, msg_id, references=None):
    m = EmailMessage()
    m["From"] = "Jürgen Example <j@example.org>"
    m["To"] = user
    m["Subject"] = subject
    m["Message-ID"] = msg_id
    if references:
        m["In-Reply-To"] = references
        m["References"] = references
    m.set_content(body)
    host, port = SMTP.split(":")
    with smtplib.SMTP(host, int(port)) as s:
        s.send_message(m)


@pytest.fixture
def backend():
    user = f"{uuid.uuid4().hex[:10]}@example.com"
    host, port = IMAP.split(":")
    acc = Account(name="test", backend="imap", label="Test", email=user, host=host, port=int(port),
                  username=user, password="pw", tls=False)
    deliver(user, "Rechnung Oktober", "Bitte bis Freitag zahlen.", "<a1@example.org>")
    deliver(user, "Grüße aus Berlin", "Hallo!", "<a2@example.org>")
    deliver(user, "Re: Rechnung Oktober", "Schon erledigt?", "<a3@example.org>", "<a1@example.org>")
    with IMAPClient(host, port=int(port), ssl=False) as c:
        c.login(user, "pw")
        c.create_folder("Drafts")
    return ImapBackend(acc, timeout=10, max_body_chars=1000)


def test_list_search_get_mark(backend):
    unread = backend.list("INBOX", False, None, None, None, 25, 1)
    assert unread["total"] == 3
    subjects = [m["subject"] for m in unread["emails"]]
    assert subjects[0] == "Re: Rechnung Oktober"                      # newest first
    assert unread["emails"][0]["from"] == "Jürgen Example <j@example.org>"
    assert unread["emails"][0]["date"].endswith("+00:00") or "+" in unread["emails"][0]["date"]

    found = backend.search("Freitag", None, 25)
    assert [m["subject"] for m in found["emails"]] == ["Rechnung Oktober"]

    first = found["emails"][0]["id"]
    mail = backend.get(first)
    assert mail["body"].startswith("Bitte bis Freitag") and mail["unread"] is True   # get does not mark read

    backend.mark(first, True)
    assert backend.list("INBOX", False, None, None, None, 25, 1)["total"] == 2
    backend.mark(first, False)
    assert backend.list("INBOX", False, None, None, None, 25, 1)["total"] == 3

    page2 = backend.list("INBOX", None, None, None, None, 2, 2)
    assert len(page2["emails"]) == 1


def test_thread_and_reply_draft(backend):
    reply = backend.search("erledigt", None, 5)["emails"][0]["id"]
    thread = backend.thread(reply)
    assert [m["subject"] for m in thread["messages"]] == ["Rechnung Oktober", "Re: Rechnung Oktober"]

    original = backend.original(reply)
    msg = build_draft(backend.acc.email, ["j@example.org"], [], "", "Ja, bezahlt.", original)
    assert backend.save_draft(msg, reply) == {"folder": "Drafts"}
    drafts = backend.list("Drafts", None, None, None, None, 5, 1)["emails"]
    assert drafts[0]["subject"] == "Re: Rechnung Oktober"
    with IMAPClient(backend.acc.host, port=backend.acc.port, ssl=False) as c:
        c.login(backend.acc.username, "pw")
        c.select_folder("Drafts", readonly=True)
        flags = c.get_flags(c.search("ALL"))
        assert all(b"\\Draft" in f for f in flags.values())


def test_stale_and_bad_ids(backend):
    mid = backend.list("INBOX", None, None, None, None, 1, 1)["emails"][0]["id"]
    box, validity, uid = mid.rsplit("|", 2)
    with pytest.raises(MailError, match="stale"):
        backend.get(f"{box}|{int(validity) + 1}|{uid}")
    with pytest.raises(MailError, match="not a message id"):
        backend.get("12345")
    with pytest.raises(MailError, match="not available"):
        backend.list("Nope", None, None, None, None, 5, 1)


def test_unreachable_server_is_an_error_not_a_crash(backend):
    backend.acc.port = 1
    with pytest.raises(MailError, match="cannot reach"):
        backend.list("INBOX", None, None, None, None, 5, 1)


def test_through_gateway_and_gate_parser(backend, env):
    """Native module behind the gateway's REST endpoint, read the way the gate scripts read it."""
    import asyncio
    import importlib.util
    import sys
    import json
    from pathlib import Path

    from scotty_gateway.app import Gateway
    from scotty_gateway.config import load
    from conftest import write_config

    acc = backend.acc
    cfg = load(write_config(env, {"email-tester": {"domain": "email", "type": "native", "module": "mail",
        "config": {"accounts": {"test": {"backend": "imap", "email": acc.email, "host": acc.host,
                                          "port": acc.port, "username": acc.username,
                                          "password": "${FAKE_SECRET}", "tls": False}}}}}))
    gw = Gateway(cfg)
    status, body = asyncio.run(gw.invoke("email-tester", "list_emails", {"account": "test", "seen": False}))
    assert status == 200 and not body["result"]["isError"]

    gate = Path(__file__).resolve().parents[3] / "templates/profile/scripts/_gateway.py"
    sys.dont_write_bytecode = True        # do not leave __pycache__ in templates/
    spec = importlib.util.spec_from_file_location("gate_gateway", gate)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    text = body["result"]["content"][0]["text"]
    items = mod.parse_email_list(json.loads(text))
    assert len(items) == 3 and all(i["unread"] and "|" in i["id"] for i in items)
    assert items[0]["subject"] == "Re: Rechnung Oktober"
