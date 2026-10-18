"""IMAP accounts (iCloud, t-online, any provider with an app password).

Moving uses IMAP MOVE (RFC 6851). Servers without it (iCloud) get the RFC 6851
fallback, restricted to what cannot lose mail: COPY first (a failure leaves the
original untouched), then \\Deleted on that one UID and UID EXPUNGE of that one
UID (UIDPLUS), so no other message is ever expunged. Without UIDPLUS moving is
refused. Nothing else in this module removes mail.

One connection per call: connect, log in, act, log out. There is no idle
connection a provider can reset under us, which is what crashed email-mcp;
a network error fails that one call and the next call starts clean.

Message ids are "<mailbox>|<uidvalidity>|<uid>": UIDs are only unique within
one mailbox and one UIDVALIDITY generation, so the id carries both.
"""
from __future__ import annotations

import re
import ssl
from contextlib import contextmanager
from datetime import date, datetime, timezone
from email.message import EmailMessage

from imapclient import IMAPClient

from .config import Account, MailError
from .parse import clip, decode_words, iso, parse_message

SEEN = b"\\Seen"
DRAFT = b"\\Draft"
DRAFT_FALLBACKS = ("Drafts", "Entwürfe", "INBOX.Drafts", "Draft")
ARCHIVE_FALLBACKS = ("Archive", "Archiv", "INBOX.Archive")
DELETED = b"\\Deleted"
# Destinations that providers empty on their own: allowed, but flagged in the action log.
TRASH_FLAGS = {"\\trash": "trash", "\\junk": "spam"}
TRASH_NAMES = re.compile(r"(?P<trash>trash|deleted|papierkorb|gel(ö|oe)scht|bin$)|(?P<spam>junk|spam)",
                         re.IGNORECASE)
THREAD_MAX_IDS = 20
SUMMARY_FIELDS = [b"ENVELOPE", b"FLAGS", b"INTERNALDATE"]


def _make_id(mailbox: str, uidvalidity: int, uid: int) -> str:
    return f"{mailbox}|{uidvalidity}|{uid}"


def split_id(msg_id: str) -> tuple[str, int, int]:
    try:
        mailbox, validity, uid = str(msg_id).rsplit("|", 2)
        return mailbox, int(validity), int(uid)
    except ValueError:
        raise MailError(f"'{msg_id}' is not a message id from this account; "
                        "use the id field of list_emails/search_emails") from None


def _address(addr) -> str:
    name = decode_words(addr.name) if addr.name else ""
    mailbox = (addr.mailbox or b"").decode("utf-8", "replace")
    host = (addr.host or b"").decode("utf-8", "replace")
    bare = f"{mailbox}@{host}" if host else mailbox
    return f"{name} <{bare}>" if name else bare


def _addresses(seq) -> str:
    return ", ".join(_address(a) for a in (seq or ()))


def _charset(criteria: list) -> str | None:
    flat = str(criteria)
    return "UTF-8" if any(ord(ch) > 127 for ch in flat) else None


class ImapBackend:
    def __init__(self, account: Account, timeout: float, max_body_chars: int):
        self.acc = account
        self.timeout = timeout
        self.max_body_chars = max_body_chars

    # -------------------------------------------------------------- plumbing
    @contextmanager
    def _client(self):
        last: Exception | None = None
        for _ in range(2):                       # one retry, for connect-time hiccups only
            try:
                client = IMAPClient(self.acc.host, port=self.acc.port, ssl=self.acc.tls,
                                    ssl_context=ssl.create_default_context(), timeout=self.timeout)
                break
            except OSError as exc:
                last = exc
        else:
            raise MailError(f"cannot reach {self.acc.host}:{self.acc.port}: {last}")
        client.normalise_times = False
        try:
            try:
                client.login(self.acc.username, self.acc.password)
            except Exception as exc:  # noqa: BLE001
                raise MailError(f"login to {self.acc.host} failed ({exc}); check the app password") from None
            yield client
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001 - the connection may already be gone
                pass

    @staticmethod
    def _select(client: IMAPClient, mailbox: str, readonly: bool = True) -> int:
        try:
            info = client.select_folder(mailbox, readonly=readonly)
        except Exception as exc:  # noqa: BLE001
            raise MailError(f"mailbox '{mailbox}' not available ({exc}); see list_mailboxes") from None
        return int(info.get(b"UIDVALIDITY", 0))

    def _summaries(self, client: IMAPClient, mailbox: str, validity: int, uids: list[int]) -> list[dict]:
        if not uids:
            return []
        data = client.fetch(uids, SUMMARY_FIELDS)
        out = []
        for uid in uids:
            item = data.get(uid)
            if not item:
                continue
            env = item.get(b"ENVELOPE")
            when = item.get(b"INTERNALDATE") or (env.date if env else None)
            out.append({
                "id": _make_id(mailbox, validity, uid),
                "from": _addresses(env.from_) if env else "",
                "to": _addresses(env.to) if env else "",
                "subject": clip(decode_words(env.subject) if env and env.subject else "(no subject)", 300),
                "date": iso(when if isinstance(when, datetime) else None),
                "unread": SEEN not in item.get(b"FLAGS", ()),
            })
        return out

    # ---------------------------------------------------------------- reads
    def mailboxes(self) -> list[dict]:
        with self._client() as c:
            return [{"name": name, "special": [f.decode() for f in flags if f.decode().lower() in
                                               ("\\drafts", "\\sent", "\\trash", "\\junk", "\\archive", "\\all")]}
                    for flags, _, name in c.list_folders()]

    def list(self, mailbox: str, seen: bool | None, subject: str | None, sender: str | None,
             since: date | None, page_size: int, page: int) -> dict:
        criteria: list = []
        if seen is False:
            criteria.append("UNSEEN")
        elif seen is True:
            criteria.append("SEEN")
        if subject:
            criteria += ["SUBJECT", subject]
        if sender:
            criteria += ["FROM", sender]
        if since:
            criteria += ["SINCE", since]
        return self._query(mailbox, criteria or ["ALL"], page_size, page)

    def search(self, query: str, mailbox: str | None, page_size: int) -> dict:
        criteria: list = []
        for word in query.split():
            criteria += ["TEXT", word]
        return self._query(mailbox or "INBOX", criteria or ["ALL"], page_size, 1)

    def _query(self, mailbox: str, criteria: list, page_size: int, page: int) -> dict:
        with self._client() as c:
            validity = self._select(c, mailbox)
            uids = sorted(c.search(criteria, charset=_charset(criteria)), reverse=True)
            start = (page - 1) * page_size
            return {"mailbox": mailbox, "total": len(uids), "page": page,
                    "emails": self._summaries(c, mailbox, validity, uids[start:start + page_size])}

    def _fetch_raw(self, c: IMAPClient, msg_id: str, readonly: bool = True) -> tuple[str, int, int, bytes, bool]:
        mailbox, validity, uid = split_id(msg_id)
        current = self._select(c, mailbox, readonly=readonly)
        if current != validity:
            raise MailError(f"message id '{msg_id}' is stale (mailbox was rebuilt); list the mailbox again")
        data = c.fetch([uid], [b"BODY.PEEK[]", b"FLAGS"]).get(uid)
        if not data or b"BODY[]" not in data:
            raise MailError(f"message '{msg_id}' not found (moved or deleted)")
        return mailbox, validity, uid, data[b"BODY[]"], SEEN not in data.get(b"FLAGS", ())

    def get(self, msg_id: str) -> dict:
        with self._client() as c:
            mailbox, _, _, raw, unread = self._fetch_raw(c, msg_id)
        out = parse_message(raw, self.max_body_chars)
        return {"id": msg_id, "mailbox": mailbox, "unread": unread, **out}

    def thread(self, msg_id: str) -> dict:
        with self._client() as c:
            mailbox, validity, uid, raw, _ = self._fetch_raw(c, msg_id)
            head = parse_message(raw, 0)
            ids = [i for i in head["references"].split() + [head["in_reply_to"], head["message_id"]] if i]
            ids = list(dict.fromkeys(ids))[-THREAD_MAX_IDS:]
            if not ids:
                return {"messages": self._summaries(c, mailbox, validity, [uid])}
            boxes = [mailbox]
            sent = c.find_special_folder(b"\\Sent")
            if sent and sent not in boxes:
                boxes.append(sent)
            found: list[dict] = []
            for box in boxes:
                validity = self._select(c, box)
                criteria = self._any_header(ids)
                uids = c.search(criteria, charset=_charset(criteria))
                found += self._summaries(c, box, validity, sorted(uids))
        found.sort(key=lambda m: m["date"])
        return {"messages": found}

    @staticmethod
    def _any_header(ids: list[str]) -> list:
        """OR over Message-ID and References for every id. IMAP's OR is a binary
        prefix operator, so n terms are `OR OR ... t1 t2 ... tn` (flat: some
        servers reject parenthesised groups)."""
        terms = [t for i in ids for t in (["HEADER", "Message-ID", i], ["HEADER", "References", i])]
        return ["OR"] * (len(terms) - 1) + [word for term in terms for word in term]

    # --------------------------------------------------------------- writes
    def _locate(self, c: IMAPClient, msg_id: str, readonly: bool) -> tuple[str, int]:
        """Select the message's mailbox and check that the id is still valid."""
        mailbox, validity, uid = split_id(msg_id)
        if self._select(c, mailbox, readonly=readonly) != validity:
            raise MailError(f"message id '{msg_id}' is stale (mailbox was rebuilt); list the mailbox again")
        if not c.search(["UID", uid]):
            raise MailError(f"message '{msg_id}' not found (moved or deleted)")
        return mailbox, uid

    def mark(self, msg_id: str, read: bool) -> None:
        with self._client() as c:
            _, uid = self._locate(c, msg_id, readonly=False)
            (c.add_flags if read else c.remove_flags)([uid], [SEEN], silent=True)

    def move(self, msg_id: str, to: str) -> dict:
        with self._client() as c:
            folders = {name: {f.decode().lower() for f in flags} for flags, _, name in c.list_folders()}
            if to not in folders and to.lower() == "archive":
                to = next((n for n, fl in folders.items() if "\\archive" in fl), None) or \
                    next((n for n in ARCHIVE_FALLBACKS if n in folders), to)
            if to not in folders:
                raise MailError(f"mailbox '{to}' not found; see list_mailboxes (folders are not created)")
            mailbox, uid = self._locate(c, msg_id, readonly=False)
            if mailbox == to:
                raise MailError(f"the mail is already in '{to}'")
            env = c.fetch([uid], [b"ENVELOPE"]).get(uid, {}).get(b"ENVELOPE")
            if c.has_capability("MOVE"):
                c.move([uid], to)
            elif c.has_capability("UIDPLUS"):
                c.copy([uid], to)                       # raises on failure: original untouched
                c.add_flags([uid], [DELETED], silent=True)
                c.uid_expunge([uid])                    # this one UID only
            else:
                raise MailError(f"{self.acc.host} supports neither MOVE nor UIDPLUS; "
                                "moving is not available for this account")
        kind = next((k for f, k in TRASH_FLAGS.items() if f in folders[to]), None)
        if kind is None and (m := TRASH_NAMES.search(to)):
            kind = "trash" if m.group("trash") else "spam"
        return {"from": mailbox, "to": to, "destination_kind": kind,
                "subject": decode_words(env.subject) if env and env.subject else "",
                "sender": _addresses(env.from_) if env else ""}

    def original(self, msg_id: str) -> dict:
        return self.get(msg_id)

    def save_draft(self, msg: EmailMessage, original_id: str | None) -> dict:
        with self._client() as c:
            folder = self.acc.drafts_folder or c.find_special_folder(b"\\Drafts")
            if not folder:
                names = {name for _, _, name in c.list_folders()}
                folder = next((n for n in DRAFT_FALLBACKS if n in names), None)
            if not folder:
                raise MailError("no drafts folder found; set drafts_folder for this account")
            c.append(folder, msg.as_bytes(), flags=[DRAFT, SEEN], msg_time=datetime.now(timezone.utc))
        return {"folder": folder}
