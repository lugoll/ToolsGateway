"""Gateway configuration: one JSON file per member, rendered by scripts/render.py.

    {
      "member": "lukas",
      "auth": {"token": "${GATEWAY_TOKEN_LUKAS}"},
      "defaults": {"timeout": 60},
      "servers": {
        "<name>": {"domain": "todoist", "type": "stdio" | "http" | "native", ...}
      }
    }

`${VAR}` anywhere in a string is replaced from the process environment. A
server that references a missing variable is not started (it is reported with
the reason); a missing auth token is fatal. Keys starting with `_` are comments.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VAR_RE = re.compile(r"\$\{(\w+)\}")
TYPES = ("stdio", "http", "native")
TOP_KEYS = {"member", "auth", "defaults", "servers", "log_level"}
COMMON_KEYS = {"domain", "type", "disabled", "note", "allow", "deny", "namespace", "timeout"}
TYPE_KEYS = {
    "stdio": {"command", "args", "env", "cwd", "mode"},
    "http": {"url", "headers", "mode"},
    "native": {"module", "config"},
}
# Backend protocol era for proxied servers. Pinned per server (not mirrored from
# the front connection): a kept-alive stdio process can only serve one era, and
# mirroring would respawn it whenever Hermes and the REST endpoint alternate.
DEFAULT_MODE = {"stdio": "legacy", "http": "auto"}


class ConfigError(ValueError):
    """The configuration file is unusable."""


@dataclass
class ServerConfig:
    name: str
    domain: str
    type: str
    disabled: bool = False
    note: str | None = None
    # Why the server is not running (disabled, missing variable, ...); None = it runs.
    reason: str | None = None
    allow: list[str] | None = None
    deny: list[str] = field(default_factory=list)
    namespace: str | None = None
    timeout: float = 60.0
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    mode: str | None = None
    module: str | None = None
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.reason is None


@dataclass
class GatewayConfig:
    member: str
    token: str
    servers: dict[str, ServerConfig]
    data_dir: Path
    log_level: str = "INFO"

    def domains(self) -> list[str]:
        out: list[str] = []
        for s in self.servers.values():
            if s.domain not in out:
                out.append(s.domain)
        return out


def substitute(value: Any, env: dict[str, str], missing: set[str]) -> Any:
    """Replace ${VAR} in every string of a JSON value; collect unknown names."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            if m.group(1) in env:
                return env[m.group(1)]
            missing.add(m.group(1))
            return m.group(0)
        return VAR_RE.sub(repl, value)
    if isinstance(value, list):
        return [substitute(v, env, missing) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, env, missing) for k, v in value.items()}
    return value


def _strip_comments(table: dict) -> dict:
    return {k: v for k, v in table.items() if not k.startswith("_")}


def _str_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where}: expected a list of strings")
    return list(value)


def _str_dict(value: Any, where: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
        raise ConfigError(f"{where}: expected a table of strings")
    return dict(value)


def parse_server(name: str, raw: dict, defaults: dict, env: dict[str, str]) -> ServerConfig:
    where = f"servers.{name}"
    if not NAME_RE.match(name):
        raise ConfigError(f"{where}: server names are lowercase letters, digits and '-'")
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table")
    raw = _strip_comments(raw)
    stype = raw.get("type")
    if stype not in TYPES:
        raise ConfigError(f"{where}.type: one of {', '.join(TYPES)}")
    unknown = set(raw) - COMMON_KEYS - TYPE_KEYS[stype]
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)} for type {stype}")
    domain = raw.get("domain")
    if not isinstance(domain, str) or not NAME_RE.match(domain):
        raise ConfigError(f"{where}.domain: required, lowercase letters, digits and '-'")

    missing: set[str] = set()
    data = substitute(raw, env, missing)
    srv = ServerConfig(
        name=name, domain=domain, type=stype,
        disabled=bool(data.get("disabled", False)),
        note=data.get("note"),
        allow=_str_list(data["allow"], f"{where}.allow") if data.get("allow") is not None else None,
        deny=_str_list(data.get("deny", []), f"{where}.deny"),
        namespace=data.get("namespace") or None,
        timeout=float(data.get("timeout", defaults.get("timeout", 60))),
        mode=data.get("mode") or DEFAULT_MODE.get(stype),
    )
    if stype == "stdio":
        if not isinstance(data.get("command"), str):
            raise ConfigError(f"{where}.command: required")
        srv.command = data["command"]
        srv.args = _str_list(data.get("args", []), f"{where}.args")
        srv.env = _str_dict(data.get("env", {}), f"{where}.env")
        srv.cwd = data.get("cwd")
    elif stype == "http":
        if not isinstance(data.get("url"), str):
            raise ConfigError(f"{where}.url: required")
        srv.url = data["url"]
        srv.headers = _str_dict(data.get("headers", {}), f"{where}.headers")
    else:
        if not isinstance(data.get("module"), str):
            raise ConfigError(f"{where}.module: required")
        srv.module = data["module"]
        cfg = data.get("config", {})
        if not isinstance(cfg, dict):
            raise ConfigError(f"{where}.config: expected a table")
        srv.config = cfg

    if srv.disabled:
        srv.reason = f"disabled: {srv.note}" if srv.note else "disabled"
    elif missing:
        srv.reason = "missing environment variables: " + ", ".join(sorted(missing))
    return srv


def load(path: str | Path, env: dict[str, str] | None = None, data_dir: str | Path | None = None) -> GatewayConfig:
    env = dict(os.environ) if env is None else env
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found") from None
    except ValueError as exc:
        raise ConfigError(f"{path}: invalid JSON ({exc})") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a JSON object")
    raw = _strip_comments(raw)
    unknown = set(raw) - TOP_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)}")
    member = raw.get("member")
    if not isinstance(member, str) or not NAME_RE.match(member):
        raise ConfigError(f"{path}: 'member' is required")

    missing: set[str] = set()
    token = substitute(_strip_comments(raw.get("auth") or {}).get("token", ""), env, missing)
    if missing or not token:
        raise ConfigError(f"{path}: auth.token is required and must resolve "
                          f"(missing: {', '.join(sorted(missing)) or 'empty value'})")
    if len(token) < 24:
        raise ConfigError(f"{path}: auth.token is shorter than 24 characters")

    defaults = _strip_comments(raw.get("defaults") or {})
    servers_raw = raw.get("servers") or {}
    if not isinstance(servers_raw, dict):
        raise ConfigError(f"{path}: 'servers' must be a table")
    servers = {n: parse_server(n, s, defaults, env) for n, s in _strip_comments(servers_raw).items()}

    namespaces: dict[tuple[str, str], str] = {}
    for s in servers.values():
        if s.namespace is None:
            continue
        key = (s.domain, s.namespace)
        if key in namespaces:
            raise ConfigError(f"servers.{s.name}: namespace '{s.namespace}' already used by "
                              f"servers.{namespaces[key]} in domain '{s.domain}'")
        namespaces[key] = s.name

    data_dir = Path(data_dir or env.get("SCOTTY_GATEWAY_DATA", "/data"))
    return GatewayConfig(member=member, token=token, servers=servers, data_dir=data_dir,
                         log_level=str(raw.get("log_level", "INFO")).upper())
