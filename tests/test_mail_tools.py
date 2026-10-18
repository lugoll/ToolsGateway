"""The tool surface and its policies, with a fake backend (no network)."""
import pytest
from fastmcp.exceptions import ToolError

from scotty_gateway.modules import ModuleContext
from scotty_gateway.modules.mail import build, tools
from scotty_gateway.modules.mail.parse import parse_message
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


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "make_backend", lambda acc, cfg, ctx: FakeBackend(acc))
    cfg = {"accounts": {
        "open": {"backend": "imap", "email": "me@example.com", "host": "h", "username": "u", "password": "p"},
        "strict": {"backend": "imap", "email": "me@example.com", "host": "h", "username": "u", "password": "p",
                   "capabilities": ["read", "draft"], "drafts": "reply-only"}}}
    import logging
    return build(cfg, ModuleContext("email-t", "t", tmp_path, logging.getLogger("t")))


async def call(server, tool, **args):
    return (await server.call_tool(tool, args)).structured_content


async def test_exact_tool_set_nothing_destructive(server):
    names = {t.name for t in await server.list_tools()}
    assert names == set(tools.TOOLS)
    assert not [n for n in names if any(word in n for word in FORBIDDEN)]
    from scotty_gateway.modules.mail.gmail_backend import GmailBackend
    from scotty_gateway.modules.mail.imap_backend import ImapBackend
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


async def test_every_change_is_in_the_action_log(server, tmp_path):
    import json
    await call(server, "move_email", account="open", id="INBOX|1|7", to="Trash")
    await call(server, "mark_email", account="open", id="INBOX|1|7", action="unread")
    await call(server, "save_draft", account="open", to="a@b.org", subject="S", body="secret body text")
    lines = [json.loads(line) for line in (tmp_path / "actions.jsonl").read_text().splitlines()]
    assert [e["action"] for e in lines] == ["move", "mark_unread", "save_draft"]
    assert lines[0]["destination_kind"] == "trash" and lines[0]["subject"] == "Grüße" and lines[0]["to"] == "Trash"
    assert "secret body text" not in (tmp_path / "actions.jsonl").read_text()
