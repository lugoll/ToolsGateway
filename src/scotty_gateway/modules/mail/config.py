"""Mail module config: the server's "config" table (recipe connectors/mail, accounts chosen per member).

    {"max_body_chars": 20000, "timeout": 20,
     "accounts": {
       "icloud": {"backend": "imap", "label": "iCloud", "email": "a@icloud.com",
                  "host": "imap.mail.me.com", "port": 993,
                  "username": "a@icloud.com", "password": "...",
                  "capabilities": ["read", "mark_read", "draft", "move"], "drafts": "any"},
       "gmail":  {"backend": "gmail", "label": "Gmail", "email": "a@gmail.com"}}}

capabilities: what the agent may do with the account. `read` lists and opens
mail; `mark_read` changes the \\Seen flag / UNREAD label; `draft` saves drafts;
`move` moves a mail to another folder / label, Trash and Spam included (every
move is written to the action log). Nothing sends or permanently deletes mail.
drafts: "any" (default) or "reply-only" (only replies, only to people already in
the thread; blunts the "Trojan draft" exfiltration path).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

CAPABILITIES = ("read", "mark_read", "draft", "move")
BACKENDS = ("imap", "gmail")
ACCOUNT_KEYS = {"backend", "label", "email", "capabilities", "drafts",
                "host", "port", "username", "password", "tls", "drafts_folder", "token_file"}
MODULE_KEYS = {"accounts", "max_body_chars", "timeout"}


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
    password: str | None = None
    tls: bool = True
    drafts_folder: str | None = None
    token_file: Path | None = None


@dataclass
class MailConfig:
    accounts: dict[str, Account]
    max_body_chars: int = 20000
    timeout: float = 20.0


def parse_config(raw: dict, data_dir: Path) -> MailConfig:
    raw = {k: v for k, v in raw.items() if not k.startswith("_")}
    unknown = set(raw) - MODULE_KEYS
    if unknown:
        raise MailConfigError(f"unknown keys {sorted(unknown)}")
    accounts = {}
    for name, a in (raw.get("accounts") or {}).items():
        a = {k: v for k, v in a.items() if not k.startswith("_")}
        where = f"accounts.{name}"
        unknown = set(a) - ACCOUNT_KEYS
        if unknown:
            raise MailConfigError(f"{where}: unknown keys {sorted(unknown)}")
        backend = a.get("backend")
        if backend not in BACKENDS:
            raise MailConfigError(f"{where}.backend: one of {', '.join(BACKENDS)}")
        caps = set(a.get("capabilities", CAPABILITIES))
        if caps - set(CAPABILITIES):
            raise MailConfigError(f"{where}.capabilities: only {', '.join(CAPABILITIES)}")
        drafts = a.get("drafts", "any")
        if drafts not in ("any", "reply-only"):
            raise MailConfigError(f"{where}.drafts: 'any' or 'reply-only'")
        acc = Account(name=name, backend=backend, label=a.get("label", name),
                      email=a.get("email") or a.get("username") or "",
                      capabilities=caps, drafts=drafts)
        if not acc.email:
            raise MailConfigError(f"{where}.email: required")
        if backend == "imap":
            for key in ("host", "username", "password"):
                if not a.get(key):
                    raise MailConfigError(f"{where}.{key}: required for imap")
            acc.host, acc.username, acc.password = a["host"], a["username"], a["password"]
            acc.port = int(a.get("port", 993))
            acc.tls = bool(a.get("tls", True))
            acc.drafts_folder = a.get("drafts_folder")
        else:
            acc.token_file = Path(a.get("token_file") or data_dir / "tokens" / f"{name}.json")
        accounts[name] = acc
    if not accounts:
        raise MailConfigError("no accounts configured")
    return MailConfig(accounts=accounts, max_body_chars=int(raw.get("max_body_chars", 20000)),
                      timeout=float(raw.get("timeout", 20)))
