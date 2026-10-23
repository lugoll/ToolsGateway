"""The tool surface and its policies, with a fake backend (no network)."""
import pytest
from fastmcp.exceptions import ToolError

from gateway.connectors import AccountRuntime, ConnectorContext
from gateway.connectors.mail import build, tools
from gateway.connectors.mail.parse import parse_message
from test_mail_parse import make

FORBIDDEN = ("send", "forward", "delete", "trash", "expunge", "schedule")


class FakeBackend:
    def __init__(self, acc, *a, **k):
        self.acc = acc
        self.drafts, self.marked = [], []

    def mailboxes(self):
        return [{"name": "INBOX", "special": []}]

    def list(self, mailbox, seen, subject, sender, since, page_size, page):
        return {"mailbox": mailbox, "emails": [{"id": "INBOX|1|7", "seen_arg": seen, "size": page_size}]}

    def search(self, query, mailbox, page_size):
        return {"mailbox": mailbox, "emails": []}

    def get(self, msg_id):
        return {"id": msg_id, **parse_message(make(), 100)}

    original = get

    def thread(self, msg_id):
        return {"messages": []}

    def mark(self, msg_id, read):
        self.marked.append((msg_id, read))

    def move(self, msg_id, to):
        kind = "trash" if to == "Trash" else None
        return {"from": "INBOX", "to": to, "destination_kind": kind, "subject": "Grüße", "sender": "j@example.org"}

    def save_draft(self, msg, original_id):
        self.drafts.append(msg)
        return {"folder": "Drafts"}


def account(name, caps=("read", "mark_read", "draft", "move"), **values):
    return AccountRuntime(name=name, label=name, backend="imap",
                          values={"email": "me@example.com", "host": "h", **values},
                          secrets={"password": "p"}, capabilities=set(caps))


@pytest.fixture
def changes():
    return []


@pytest.fixture
def server(monkeypatch, changes):
    monkeypatch.setattr(tools, "make_backend", lambda acc, cfg, ctx: FakeBackend(acc))
    import logging
    ctx = ConnectorContext("t", "mail", logging.getLogger("t"),
                           record_change=lambda acc, action, fields: changes.append((acc, action, fields)))
    return build([account("open"), account("strict", ("read", "draft"), drafts="reply-only"),
                  AccountRuntime(name="nopw", label="x", backend="imap", values={"email": "a@b.c", "host": "h"},
                                 secrets={}, capabilities={"read"})], ctx)


async def call(server, tool, **args):
    return (await server.call_tool(tool, args)).structured_content


async def test_exact_tool_set_nothing_destructive(server):
    names = {t.name for t in await server.list_tools()}
    assert names == set(tools.TOOLS)
    assert not [n for n in names if any(word in n for word in FORBIDDEN)]
    from gateway.connectors.mail.gmail import GmailBackend
    from gateway.connectors.mail.imap import ImapBackend
    for cls in (ImapBackend, GmailBackend):          # the real backends have no such method either
        assert not [m for m in dir(cls) if any(word in m.lower() for word in FORBIDDEN)]


async def test_list_passes_gate_arguments(server):
    out = await call(server, "list_emails", account="open", seen=False, pageSize=5)
    assert out["emails"][0]["seen_arg"] is False and out["emails"][0]["size"] == 5


async def test_account_required_when_several(server):
    with pytest.raises(ToolError, match="name the account"):
        await call(server, "list_emails")


async def test_capability_blocks_mark(server):
    with pytest.raises(ToolError, match="does not allow mark read"):
        await call(server, "mark_email", account="strict", id="INBOX|1|7")
    assert (await call(server, "mark_email", account="open", id="INBOX|1|7"))["marked"] == "read"


async def test_reply_only_policy(server):
    with pytest.raises(ToolError, match="only allows reply drafts"):
        await call(server, "save_draft", account="strict", to="x@evil.org", body="b")
    with pytest.raises(ToolError, match="x@evil.org"):
        await call(server, "save_draft", account="strict", to=["j@example.org", "x@evil.org"],
                   body="b", in_reply_to="INBOX|1|7")
    out = await call(server, "save_draft", account="strict", to="j@example.org", body="b", in_reply_to="INBOX|1|7")
    assert out["saved"] and out["sent"] is False and out["threaded"] and out["subject"] == "Re: Grüße"


async def test_open_drafts_any_recipient(server):
    out = await call(server, "save_draft", account="open", to="new@example.net", subject="Hi", body="b")
    assert out["saved"] and not out["threaded"]


async def test_move_needs_capability(server):
    with pytest.raises(ToolError, match="does not allow move"):
        await call(server, "move_email", account="strict", id="INBOX|1|7", to="Archive")
    out = await call(server, "move_email", account="open", id="INBOX|1|7", to="Archive")
    assert out["moved"] and out["to"] == "Archive" and out["destination_kind"] is None


async def test_every_change_is_recorded(server, changes):
    await call(server, "move_email", account="open", id="INBOX|1|7", to="Trash")
    await call(server, "mark_email", account="open", id="INBOX|1|7", action="unread")
    await call(server, "save_draft", account="open", to="a@b.org", subject="S", body="secret body text")
    assert [(acc, action) for acc, action, _ in changes] == [("open", "move"), ("open", "mark_unread"),
                                                              ("open", "save_draft")]
    move = changes[0][2]
    assert move["destination_kind"] == "trash" and move["subject"] == "Grüße" and move["to"] == "Trash"
    assert "secret body text" not in str(changes)


async def test_broken_account_is_listed_not_fatal(server):
    accounts = {a["account"]: a for a in (await call(server, "list_accounts"))["accounts"]}
    assert accounts["nopw"]["status"] == "not usable: account nopw: no password set"
    assert accounts["open"]["allows"] == ["draft", "mark_read", "move", "read"]
    with pytest.raises(ToolError, match="not usable"):
        await call(server, "list_emails", account="nopw")
