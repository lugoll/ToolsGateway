"""IMAP backend against a real server. Needs GreenMail:

    docker run -d --rm --name gateway-greenmail \
      -e GREENMAIL_OPTS='-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.auth.disabled' \
      -p 127.0.0.1:3025:3025 -p 127.0.0.1:3143:3143 greenmail/standalone:2.1.14
    GATEWAY_TEST_IMAP=127.0.0.1:3143 GATEWAY_TEST_SMTP=127.0.0.1:3025 uv run pytest -m imap
"""
import os
import smtplib
import uuid
from email.message import EmailMessage

import pytest
from imapclient import IMAPClient

from gateway.connectors.mail.config import Account, MailError
from gateway.connectors.mail.imap import ImapBackend
from gateway.connectors.mail.parse import build_draft

IMAP = os.environ.get("GATEWAY_TEST_IMAP")
SMTP = os.environ.get("GATEWAY_TEST_SMTP")
pytestmark = [pytest.mark.imap, pytest.mark.skipif(not (IMAP and SMTP), reason="GATEWAY_TEST_IMAP/SMTP unset")]


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
        c.create_folder("Archiv")
        c.create_folder("Trash")
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


async def test_through_gateway(backend, services):
    """The IMAP account as a connection of a user, called through the runtime's REST shape."""
    acc = backend.acc
    user = services.users.create("tester", "tester-password")
    services.connections.create(user.id, services.catalog.get("imap-mail"), "test", "Test",
                                {"email": acc.email, "host": acc.host, "port": acc.port, "tls": False},
                                {"password": "pw"}, by="tester")
    runtime = services.registry.build(user)
    status, body = await runtime.invoke("mail", "list_emails", {"account": "test", "seen": False})
    assert status == 200 and not body["result"]["isError"]
    emails = body["result"]["structuredContent"]["emails"]
    assert len(emails) == 3 and all(e["unread"] and "|" in e["id"] for e in emails)
    assert emails[0]["subject"] == "Re: Rechnung Oktober"


def test_move(backend):
    mail = backend.search("Freitag", None, 5)["emails"][0]
    if not _has_move(backend):
        with pytest.raises(MailError, match="does not support IMAP MOVE"):
            backend.move(mail["id"], "Archiv")
        pytest.skip("server lacks MOVE: refusal verified, move itself untested here")
    with pytest.raises(MailError, match="not found"):
        backend.move(mail["id"], "Nowhere")
    out = backend.move(mail["id"], "archive")                                   # fallback name
    assert out == {"from": "INBOX", "to": "Archiv", "destination_kind": None,
                   "subject": "Rechnung Oktober", "sender": "Jürgen Example <j@example.org>"}
    assert backend.list("INBOX", None, None, None, None, 25, 1)["total"] == 2
    moved = backend.list("Archiv", None, None, None, None, 5, 1)["emails"]
    assert [m["subject"] for m in moved] == ["Rechnung Oktober"] and moved[0]["unread"]
    with pytest.raises(MailError, match="not found"):
        backend.move(mail["id"], "Archiv")                     # old id is gone from INBOX


def _has_move(backend) -> bool:
    with IMAPClient(backend.acc.host, port=backend.acc.port, ssl=False) as c:
        return c.has_capability("MOVE")


def test_move_to_trash_is_flagged(backend):
    mail = backend.search("Berlin", None, 5)["emails"][0]
    assert backend.move(mail["id"], "Trash")["destination_kind"] == "trash"
    assert [m["subject"] for m in backend.list("Trash", None, None, None, None, 5, 1)["emails"]] == ["Grüße aus Berlin"]


def test_move_fallback_without_move_capability(backend, monkeypatch):
    """iCloud: no MOVE, but UIDPLUS. Copy, then expunge exactly that one UID."""
    real = IMAPClient.has_capability
    monkeypatch.setattr(IMAPClient, "has_capability",
                        lambda self, cap: False if cap.upper() == "MOVE" else real(self, cap))
    if not _has_capability(backend, "UIDPLUS"):
        pytest.skip("test server lacks UIDPLUS")
    # A bystander already flagged \Deleted in INBOX must survive the move (plain EXPUNGE would remove it).
    host, port = backend.acc.host, backend.acc.port
    bystander = backend.search("Berlin", None, 5)["emails"][0]["id"].rsplit("|", 1)[1]
    with IMAPClient(host, port=port, ssl=False) as c:
        c.login(backend.acc.username, "pw")
        c.select_folder("INBOX")
        c.add_flags([int(bystander)], [b"\\Deleted"])
    mail = backend.search("Freitag", None, 5)["emails"][0]
    assert backend.move(mail["id"], "Archiv")["to"] == "Archiv"
    assert [m["subject"] for m in backend.list("Archiv", None, None, None, None, 5, 1)["emails"]] == ["Rechnung Oktober"]
    subjects = [m["subject"] for m in backend.list("INBOX", None, None, None, None, 25, 1)["emails"]]
    assert "Rechnung Oktober" not in subjects and "Grüße aus Berlin" in subjects


def _has_capability(backend, cap) -> bool:
    with IMAPClient(backend.acc.host, port=backend.acc.port, ssl=False) as c:
        c.login(backend.acc.username, "pw")
        return c.has_capability(cap)


# ------------------------------------------------------- kept connection
@pytest.fixture
def logins(monkeypatch):
    count = []
    real = IMAPClient.login
    monkeypatch.setattr(IMAPClient, "login", lambda self, *a: (count.append(1), real(self, *a))[1])
    return count


def reset(backend) -> None:
    """What a provider closing an idle connection looks like to us."""
    import socket
    backend._conn.socket().shutdown(socket.SHUT_RDWR)


def inbox_total(backend) -> int:
    return backend.list("INBOX", None, None, None, None, 5, 1)["total"]


def test_connection_is_kept_and_checked(backend, logins):
    assert [inbox_total(backend) for _ in range(3)] == [3, 3, 3] and len(logins) == 1


def test_connection_reset_by_the_provider_is_replaced(backend, logins):
    inbox_total(backend)
    reset(backend)
    assert inbox_total(backend) == 3 and len(logins) == 2


def test_failed_reconnect_is_an_error(backend):
    inbox_total(backend)
    reset(backend)
    backend.acc.port = 1
    with pytest.raises(MailError, match="cannot reach"):
        inbox_total(backend)


def test_failure_during_a_call_drops_the_connection(backend, logins, monkeypatch):
    inbox_total(backend)
    monkeypatch.setattr(backend._conn, "search", lambda *a, **k: (_ for _ in ()).throw(OSError("reset")))
    with pytest.raises(OSError):
        inbox_total(backend)
    assert backend._conn is None
    assert inbox_total(backend) == 3 and len(logins) == 2


def test_parallel_calls_share_one_connection_in_turn(backend, logins):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(4) as pool:
        assert list(pool.map(lambda _: inbox_total(backend), range(8))) == [3] * 8
    assert len(logins) == 1


def test_idle_connection_is_logged_out(backend, monkeypatch):
    import time
    from gateway.connectors.mail import imap
    monkeypatch.setattr(imap, "IDLE_CLOSE", 0.2)
    inbox_total(backend)
    time.sleep(0.6)
    assert backend._conn is None
    assert inbox_total(backend) == 3
