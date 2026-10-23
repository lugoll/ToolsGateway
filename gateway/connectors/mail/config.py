"""Mail accounts from connections (catalog entries icloud-mail, gmail, imap, ...).

Values per account (catalog settings + the user's fields):
    imap:  email, host, port (993), username (default: email), tls (true), drafts_folder
           secret password
    gmail: email; secret oauth_token (authorized-user JSON from the Google flow)
    both:  drafts = "any" | "reply-only"

capabilities: what the agent may do with the account. `read` lists and opens
mail; `mark_read` changes the \\Seen flag / UNREAD label; `draft` saves drafts;
`move` moves a mail to another folder / label, Trash and Spam included (every
move is written to the audit log). Nothing sends or permanently deletes mail.
drafts: "any" (default) or "reply-only" (only replies, only to people already in
the thread; blunts the "Trojan draft" exfiltration path).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import AccountRuntime

CAPABILITIES = ("read", "mark_read", "draft", "move")
BACKENDS = {"imap", "gmail"}


class MailConfigError(ValueError):
    pass


class MailError(RuntimeError):
    """A failure worth showing to the agent as is (no secrets in the text)."""


@dataclass
class Account:
    name: str
    backend: str
    label: str
    email: str
    capabilities: set[str] = field(default_factory=lambda: set(CAPABILITIES))
    drafts: str = "any"
    host: str | None = None
    port: int = 993
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    tls: bool = True
    drafts_folder: str | None = None
    token: str | None = field(default=None, repr=False)


@dataclass
class MailConfig:
    accounts: dict[str, Account]
    max_body_chars: int = 20000
    timeout: float = 20.0


def _bool(value) -> bool:
    return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")


def account_from_runtime(rt: AccountRuntime) -> Account:
    v = rt.values
    where = f"account {rt.name}"
    if rt.backend not in BACKENDS:
        raise MailConfigError(f"{where}: backend one of {', '.join(sorted(BACKENDS))}")
    caps = set(rt.capabilities)
    if caps - set(CAPABILITIES):
        raise MailConfigError(f"{where}: capabilities only {', '.join(CAPABILITIES)}")
    drafts = v.get("drafts") or "any"
    if drafts not in ("any", "reply-only"):
        raise MailConfigError(f"{where}: drafts 'any' or 'reply-only'")
    acc = Account(name=rt.name, backend=rt.backend, label=rt.label or rt.name,
                  email=str(v.get("email") or v.get("username") or ""), capabilities=caps, drafts=drafts)
    if not acc.email:
        raise MailConfigError(f"{where}: email is required")
    if rt.backend == "imap":
        acc.host = v.get("host")
        acc.username = str(v.get("username") or acc.email)
        acc.password = rt.secrets.get("password")
        if not acc.host:
            raise MailConfigError(f"{where}: host is required for imap")
        if not acc.password:
            raise MailConfigError(f"{where}: no password set")
        acc.port = int(v.get("port") or 993)
        acc.tls = _bool(v.get("tls", True))
        acc.drafts_folder = v.get("drafts_folder") or None
    else:
        acc.token = rt.secrets.get("oauth_token")
    return acc
