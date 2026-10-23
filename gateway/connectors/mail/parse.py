"""RFC 5322 messages in and out: shared by the IMAP and Gmail backends.

Everything an LLM reads goes through here: headers are decoded, HTML bodies are
converted to text, bodies are capped, attachments are described but never
returned.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage

from inscriptis import get_text

SUBJECT_MAX = 300


def decode_words(value) -> str:
    """Decode RFC 2047 encoded words (bytes or str) into one clean line."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    try:
        value = str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - malformed encoded words: keep the raw text
        pass
    return " ".join(value.split())


def clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="seconds")


def addresses(*values: str | None) -> set[str]:
    """Lower-cased bare addresses from any number of address headers."""
    return {addr.lower() for _, addr in email.utils.getaddresses([v for v in values if v]) if addr}


def html_to_text(html: str) -> str:
    try:
        return get_text(html)
    except Exception:  # noqa: BLE001 - broken markup: fall back to the raw source
        return html


def parse_message(raw: bytes, max_chars: int) -> dict:
    """Headers, a text body and attachment metadata of one message."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body, fmt = "", "none"
    if body_part is not None:
        try:
            content = body_part.get_content()
        except Exception:  # noqa: BLE001 - unknown charset etc.
            payload = body_part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", "replace")
        fmt = body_part.get_content_subtype()
        body = html_to_text(content) if fmt == "html" else content
    body = "\n".join(line.rstrip() for line in body.strip().splitlines())
    truncated = len(body) > max_chars
    attachments = []
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True) or b""
        attachments.append({"filename": decode_words(part.get_filename()) or "(unnamed)",
                            "content_type": part.get_content_type(), "size": len(payload)})
    date = None
    try:
        date = email.utils.parsedate_to_datetime(str(msg["Date"])) if msg["Date"] else None
    except (TypeError, ValueError):
        pass

    def h(name: str) -> str:
        return decode_words(str(msg[name])) if msg[name] is not None else ""

    return {
        "subject": clip(h("Subject") or "(no subject)", SUBJECT_MAX),
        "from": h("From"), "to": h("To"), "cc": h("Cc"), "reply_to": h("Reply-To"),
        "date": iso(date),
        "message_id": h("Message-ID"), "in_reply_to": h("In-Reply-To"), "references": h("References"),
        "body": body[:max_chars], "body_format": fmt, "truncated": truncated,
        "attachments": attachments,
    }


def build_draft(sender: str, to: list[str], cc: list[str], subject: str, body: str,
                original: dict | None) -> EmailMessage:
    """A draft, threaded under `original` (a parse_message result) when given."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if original:
        base = original["subject"]
        subject = subject or (base if base.lower().startswith(("re:", "aw:")) else f"Re: {base}")
        if original.get("message_id"):
            msg["In-Reply-To"] = original["message_id"]
            refs = original.get("references", "").split()
            msg["References"] = " ".join(refs + [original["message_id"]])
    msg["Subject"] = subject or "(no subject)"
    msg["Date"] = email.utils.formatdate(localtime=True)
    domain = sender.rsplit("@", 1)[-1].strip(">") if "@" in sender else None
    msg["Message-ID"] = email.utils.make_msgid(domain=domain)
    msg.set_content(body)
    return msg
