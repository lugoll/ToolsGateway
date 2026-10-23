"""Instance settings, read once from the environment.

Everything a user can change lives in the database and is edited in the web UI;
the environment only holds what must exist before the database can be opened
(the master key) or what belongs to the deployment (paths, public URL, the
first admin).

    GATEWAY_MASTER_KEY        base64, 32 bytes (`gateway keygen`); or GATEWAY_MASTER_KEY_FILE
    GATEWAY_MASTER_KEY_OLD    previous key while rotating (`gateway rotate-key`)
    GATEWAY_DATA              writable directory: gateway.db (default ./data)
    GATEWAY_CATALOG           directory of catalog/*.yaml (default: the repo's catalog/)
    GATEWAY_PUBLIC_URL        how browsers reach the UI, e.g. https://tools.pi.home
    GATEWAY_ADMIN_USER        first admin, created only when no admin exists
    GATEWAY_ADMIN_PASSWORD
    GATEWAY_AUDIT_DAYS        audit retention in days (default 90)
    GATEWAY_AUDIT_MAX_ROWS    audit retention in rows (default 100000)
    GATEWAY_HOST / _PORT      bind address (default 0.0.0.0:3050)
    GATEWAY_LOG_LEVEL         default INFO
"""
from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from pathlib import Path

REPO_CATALOG = Path(__file__).resolve().parent.parent / "catalog"


class SettingsError(ValueError):
    """The environment is unusable; the message says which variable."""


def parse_key(value: str, name: str) -> bytes:
    value = value.strip()
    try:
        key = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != 32:
        raise SettingsError(f"{name}: expected 32 bytes, base64 encoded (create one with `gateway keygen`)")
    return key


def _key(env: dict[str, str], name: str, required: bool) -> bytes | None:
    value = env.get(name)
    path = env.get(f"{name}_FILE")
    if not value and path:
        try:
            value = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise SettingsError(f"{name}_FILE: {exc}") from None
    if not value:
        if required:
            raise SettingsError(f"{name} is not set (create one with `gateway keygen`)")
        return None
    return parse_key(value, name)


def _int(env: dict[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name) or default)
    except ValueError:
        raise SettingsError(f"{name}: expected a number") from None


@dataclass(frozen=True)
class Settings:
    master_key: bytes
    master_key_old: bytes | None
    data_dir: Path
    catalog_dir: Path
    public_url: str
    admin_user: str | None
    admin_password: str | None
    audit_days: int = 90
    audit_max_rows: int = 100_000
    host: str = "0.0.0.0"
    port: int = 3050
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "gateway.db"

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        return cls(
            master_key=_key(env, "GATEWAY_MASTER_KEY", required=True),
            master_key_old=_key(env, "GATEWAY_MASTER_KEY_OLD", required=False),
            data_dir=Path(env.get("GATEWAY_DATA") or "data"),
            catalog_dir=Path(env.get("GATEWAY_CATALOG") or REPO_CATALOG),
            public_url=(env.get("GATEWAY_PUBLIC_URL") or "http://localhost:3050").rstrip("/"),
            admin_user=env.get("GATEWAY_ADMIN_USER") or None,
            admin_password=env.get("GATEWAY_ADMIN_PASSWORD") or None,
            audit_days=_int(env, "GATEWAY_AUDIT_DAYS", 90),
            audit_max_rows=_int(env, "GATEWAY_AUDIT_MAX_ROWS", 100_000),
            host=env.get("GATEWAY_HOST") or "0.0.0.0",
            port=_int(env, "GATEWAY_PORT", 3050),
            log_level=(env.get("GATEWAY_LOG_LEVEL") or "INFO").upper(),
        )
