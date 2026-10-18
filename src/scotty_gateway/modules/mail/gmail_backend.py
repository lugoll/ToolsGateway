"""Gmail accounts through the Gmail REST API with OAuth (scope gmail.modify).

Why not IMAP: Gmail's IMAP accepts OAuth only with the full-access scope
https://mail.google.com/ (permanent delete, settings, everything). gmail.modify
covers reading, labels (mark read) and drafts, and excludes permanent deletion
and settings such as forwarding rules. It does permit sending; this module has
no code that sends, and the token never leaves this container.

Mailboxes are labels (INBOX, user labels); unread is the UNREAD label; message
ids are Gmail's own ids. Moving a mail = adding the destination label and
removing INBOX and its other user labels; "archive" removes them without adding
one. TRASH goes through messages.trash (recoverable for 30 days, never the
permanent delete), SPAM is a label change; both are flagged in the action log. The refresh token lives in <data>/tokens/<account>.json
(written by `scotty-gateway auth`, mode 0600).
"""
from __future__ import annotations

import base64
from datetime import date, datetime, timezone
from email.message import EmailMessage

from .config import Account, MailError
from .parse import clip, decode_words, iso, parse_message

SCOPE = "https://www.googleapis.com/auth/gmail.modify"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SUMMARY_HEADERS = ("From", "To", "Subject", "Date")


class NeedsAuth(MailError):
    pass


class GmailBackend:
    def __init__(self, account: Account, timeout: float, max_body_chars: int, server: str):
        self.acc = account
        self.timeout = timeout
        self.max_body_chars = max_body_chars
        self.server = server
        self._session = None

    # -------------------------------------------------------------- plumbing
    def _auth_hint(self) -> str:
        return (f"re-auth needed for {self.acc.name}: run "
                f"`scotty-gateway auth {self.server} {self.acc.name}` on the host")

    def session(self):
        if self._session is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2.credentials import Credentials
            if not self.acc.token_file or not self.acc.token_file.exists():
                raise NeedsAuth(self._auth_hint())
            try:
                creds = Credentials.from_authorized_user_file(str(self.acc.token_file), [SCOPE])
            except ValueError as exc:
                raise NeedsAuth(f"{self._auth_hint()} ({exc})") from None
            self._session = AuthorizedSession(creds)
        return self._session

    def _call(self, method: str, path: str, **kwargs) -> dict:
        from google.auth.exceptions import RefreshError
        try:
            resp = self.session().request(method, f"{API}{path}", timeout=self.timeout, **kwargs)
        except RefreshError as exc:
            self._session = None
            raise NeedsAuth(f"{self._auth_hint()} ({exc})") from None
        except OSError as exc:
            raise MailError(f"Gmail API unreachable: {exc}") from None
        if resp.status_code == 401:
            self._session = None
            raise NeedsAuth(self._auth_hint())
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error", {}).get("message", resp.text)
            except ValueError:
                detail = resp.text
            raise MailError(f"Gmail API {resp.status_code}: {clip(str(detail), 300)}")
        return resp.json() if resp.content else {}

    def token_present(self) -> bool:
        return bool(self.acc.token_file and self.acc.token_file.exists())

    def _labels(self) -> list[dict]:
        return self._call("GET", "/labels").get("labels", [])

    def _label_id(self, mailbox: str, labels: list[dict] | None = None) -> str:
        labels = self._labels() if labels is None else labels
        for lab in labels:
            if mailbox in (lab["id"], lab["name"]) or mailbox.lower() == lab["name"].lower():
                return lab["id"]
        raise MailError(f"mailbox (label) '{mailbox}' not found; see list_mailboxes")

    def _summary(self, msg_id: str) -> dict:
        m = self._call("GET", f"/messages/{msg_id}",
                       params=[("format", "metadata")] + [("metadataHeaders", h) for h in SUMMARY_HEADERS])
        return self._to_summary(m)

    @staticmethod
    def _to_summary(m: dict) -> dict:
        headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        when = datetime.fromtimestamp(int(m.get("internalDate", 0)) / 1000, tz=timezone.utc)
        return {
            "id": m["id"],
            "thread_id": m.get("threadId"),
            "from": decode_words(headers.get("from")),
            "to": decode_words(headers.get("to")),
            "subject": clip(decode_words(headers.get("subject")) or "(no subject)", 300),
            "date": iso(when),
            "unread": "UNREAD" in m.get("labelIds", []),
            "snippet": clip(decode_words(m.get("snippet")), 200),
        }

    def _query(self, q: str, label: str | None, page_size: int, page: int) -> dict:
        params: list = [("maxResults", page_size)]
        if q:
            params.append(("q", q))
        if label:
            params.append(("labelIds", label))
        token, result = None, {}
        for _ in range(page):
            result = self._call("GET", "/messages", params=params + ([("pageToken", token)] if token else []))
            token = result.get("nextPageToken")
            if not token:
                break
        ids = [m["id"] for m in result.get("messages", [])] if result else []
        return {"total_estimate": result.get("resultSizeEstimate", len(ids)), "page": page,
                "emails": [self._summary(i) for i in ids]}

    # ---------------------------------------------------------------- reads
    def mailboxes(self) -> list[dict]:
        return [{"name": lab["name"], "id": lab["id"], "type": lab.get("type", "")} for lab in self._labels()]

    def list(self, mailbox: str, seen: bool | None, subject: str | None, sender: str | None,
             since: date | None, page_size: int, page: int) -> dict:
        terms = []
        if seen is False:
            terms.append("is:unread")
        elif seen is True:
            terms.append("-is:unread")
        if subject:
            terms.append(f'subject:"{subject}"')
        if sender:
            terms.append(f'from:"{sender}"')
        if since:
            terms.append(f"after:{since:%Y/%m/%d}")
        out = self._query(" ".join(terms), self._label_id(mailbox), page_size, page)
        return {"mailbox": mailbox, **out}

    def search(self, query: str, mailbox: str | None, page_size: int) -> dict:
        label = self._label_id(mailbox) if mailbox else None
        return {"mailbox": mailbox or "(all mail)", **self._query(query, label, page_size, 1)}

    def _raw(self, msg_id: str) -> tuple[dict, bytes]:
        m = self._call("GET", f"/messages/{msg_id}", params={"format": "raw"})
        return m, base64.urlsafe_b64decode(m["raw"] + "=" * (-len(m["raw"]) % 4))

    def get(self, msg_id: str) -> dict:
        m, raw = self._raw(msg_id)
        labels = m.get("labelIds", [])
        return {"id": msg_id, "thread_id": m.get("threadId"), "labels": labels,
                "unread": "UNREAD" in labels, **parse_message(raw, self.max_body_chars)}

    def thread(self, msg_id: str) -> dict:
        tid = self._call("GET", f"/messages/{msg_id}", params={"format": "minimal"})["threadId"]
        t = self._call("GET", f"/threads/{tid}",
                       params=[("format", "metadata")] + [("metadataHeaders", h) for h in SUMMARY_HEADERS])
        msgs = [self._to_summary(m) for m in t.get("messages", [])]
        msgs.sort(key=lambda m: m["date"])
        return {"thread_id": tid, "messages": msgs}

    # --------------------------------------------------------------- writes
    def mark(self, msg_id: str, read: bool) -> None:
        body = {"removeLabelIds": ["UNREAD"]} if read else {"addLabelIds": ["UNREAD"]}
        self._call("POST", f"/messages/{msg_id}/modify", json=body)

    def move(self, msg_id: str, to: str) -> dict:
        labels = self._labels()
        names = {lab["id"]: lab["name"] for lab in labels}
        target = None if to.lower() == "archive" else self._label_id(to, labels)
        meta = self._call("GET", f"/messages/{msg_id}",
                          params=[("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject")])
        current = meta.get("labelIds", [])
        location = {"INBOX", "SPAM"} | {lab["id"] for lab in labels if lab.get("type") == "user"}
        remove = [lab for lab in current if lab in location and lab != target]
        if target in current and not remove:
            raise MailError(f"the mail is already in '{to}'")
        if target == "TRASH":
            self._call("POST", f"/messages/{msg_id}/trash")
        else:
            if "TRASH" in current:                      # a label change alone leaves it in Trash
                self._call("POST", f"/messages/{msg_id}/untrash")
            body: dict = {"removeLabelIds": remove}
            if target:
                body["addLabelIds"] = [target]
            self._call("POST", f"/messages/{msg_id}/modify", json=body)
        summary = self._to_summary(meta)
        return {"from": [names.get(lab, lab) for lab in remove], "to": names.get(target, "archive"),
                "destination_kind": {"TRASH": "trash", "SPAM": "spam"}.get(target),
                "subject": summary["subject"], "sender": summary["from"]}

    def original(self, msg_id: str) -> dict:
        return self.get(msg_id)

    def save_draft(self, msg: EmailMessage, original_id: str | None) -> dict:
        message: dict = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if original_id:
            message["threadId"] = self._call("GET", f"/messages/{original_id}",
                                             params={"format": "minimal"})["threadId"]
        draft = self._call("POST", "/drafts", json={"message": message})
        return {"folder": "Drafts", "draft_id": draft.get("id")}
