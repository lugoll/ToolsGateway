"""Gateway configuration: the member's connector list plus the recipes it names.

    # /etc/scotty-gateway/members/lukas.toml   (repo: config/gateway/lukas.toml)
    member = "lukas"
    [[connectors]]
    recipe = "todoist"
    [[connectors]]
    recipe = "mail"
    accounts = ["icloud", "gmail"]
    [[connectors]]
    recipe = "caldav-google"
    disabled = true
    note = "why, and what re-enables it"

    # /etc/scotty-gateway/connectors/<recipe>/recipe.toml   (repo: connectors/)
    name   = "todoist"            # server "todoist-<member>"
    domain = "todoist"            # endpoint /mcp/todoist
    [server]
    type = "stdio" | "http" | "native"   (+ the keys of that type, allow, deny, ...)
    [accounts.<id>]               # optional: the chosen ones become server.config.accounts

Optional in the member file: `[auth] token` (default "${GATEWAY_TOKEN}"),
`[defaults] timeout`, `log_level`. `${VAR}` anywhere in a string is replaced
from the process environment. A server that references a missing variable is
not started (it is reported with the reason); a missing auth token is fatal.
Keys starting with `_` are comments. Every recipe's domain is served, also when
this member uses none of its connectors (an empty endpoint), so one Hermes
policy can list the household's domains for every member.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VAR_RE = re.compile(r"\$\{(\w+)\}")
TYPES = ("stdio", "http", "native")
MEMBER_KEYS = {"member", "auth", "defaults", "connectors", "log_level"}
ENTRY_KEYS = {"recipe", "accounts", "disabled", "note"}
RECIPE_KEYS = {"name", "domain", "doc", "server", "accounts"}
DEFAULT_TOKEN = "${GATEWAY_TOKEN}"
DEFAULT_CONNECTORS = "/etc/scotty-gateway/connectors"
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
    # Domains of recipes this member does not use: served as empty endpoints.
    extra_domains: list[str] = field(default_factory=list)

    def domains(self) -> list[str]:
        out: list[str] = []
        for d in [s.domain for s in self.servers.values()] + self.extra_domains:
            if d not in out:
                out.append(d)
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


def _read_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML ({exc})") from None


def load_recipe(connectors_dir: Path, rid: str) -> dict:
    path = connectors_dir / rid / "recipe.toml"
    raw = _strip_comments(_read_toml(path))
    unknown = set(raw) - RECIPE_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)}")
    for key in ("name", "domain"):
        if not isinstance(raw.get(key), str) or not NAME_RE.match(raw[key]):
            raise ConfigError(f"{path}: '{key}' is required, lowercase letters, digits and '-'")
    if not isinstance(raw.get("server"), dict):
        raise ConfigError(f"{path}: a [server] table is required")
    if not isinstance(raw.get("accounts", {}), dict):
        raise ConfigError(f"{path}: 'accounts' must be a table of tables")
    return raw


def expand_connector(member: str, entry: Any, recipe: dict, where: str) -> tuple[str, dict]:
    """One `[[connectors]]` entry -> (server name, raw server table)."""
    if not isinstance(entry, dict):
        raise ConfigError(f"{where}: expected a table")
    unknown = set(entry) - ENTRY_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    raw = dict(recipe["server"])
    raw["domain"] = recipe["domain"]
    offered = recipe.get("accounts")
    if offered:
        chosen = _str_list(entry.get("accounts", []), f"{where}.accounts")
        missing = [a for a in chosen if a not in offered]
        if not chosen or missing:
            raise ConfigError(f"{where}.accounts: pick from {', '.join(offered)}"
                              + (f" (unknown: {', '.join(missing)})" if missing else ""))
        raw["config"] = {**raw.get("config", {}), "accounts": {a: dict(offered[a]) for a in chosen}}
    elif "accounts" in entry:
        raise ConfigError(f"{where}.accounts: recipe '{entry['recipe']}' has no accounts")
    if entry.get("disabled"):
        if not entry.get("note"):
            raise ConfigError(f"{where}: a disabled connector needs a 'note' (shown to the agent)")
        raw["disabled"], raw["note"] = True, entry["note"]
    return f"{recipe['name']}-{member}", raw


def load(path: str | Path, env: dict[str, str] | None = None, data_dir: str | Path | None = None,
         connectors_dir: str | Path | None = None) -> GatewayConfig:
    env = dict(os.environ) if env is None else env
    path = Path(path)
    raw = _strip_comments(_read_toml(path))
    unknown = set(raw) - MEMBER_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)}")
    member = raw.get("member")
    if not isinstance(member, str) or not NAME_RE.match(member):
        raise ConfigError(f"{path}: 'member' is required")

    missing: set[str] = set()
    token = substitute(_strip_comments(raw.get("auth") or {}).get("token", DEFAULT_TOKEN), env, missing)
    if missing or not token:
        raise ConfigError(f"{path}: auth.token is required and must resolve "
                          f"(missing: {', '.join(sorted(missing)) or 'empty value'})")
    if len(token) < 24:
        raise ConfigError(f"{path}: auth.token is shorter than 24 characters")

    cdir = Path(connectors_dir or env.get("SCOTTY_GATEWAY_CONNECTORS") or DEFAULT_CONNECTORS)
    if not cdir.is_dir():
        raise ConfigError(f"{cdir}: connectors directory not found")
    recipes = {d.name: load_recipe(cdir, d.name) for d in sorted(cdir.iterdir())
               if (d / "recipe.toml").is_file()}

    defaults = _strip_comments(raw.get("defaults") or {})
    entries = raw.get("connectors") or []
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: 'connectors' must be an array of tables ([[connectors]])")
    servers_raw: dict[str, dict] = {}
    for i, entry in enumerate(entries):
        where = f"{path}: connectors[{i}]"
        rid = entry.get("recipe") if isinstance(entry, dict) else None
        if rid not in recipes:
            raise ConfigError(f"{where}.recipe: one of {', '.join(recipes)}")
        name, server = expand_connector(member, entry, recipes[rid], where)
        if name in servers_raw:
            raise ConfigError(f"{where}: recipe '{rid}' is listed twice")
        servers_raw[name] = server
    servers = {n: parse_server(n, s, defaults, env) for n, s in servers_raw.items()}

    namespaces: dict[tuple[str, str], str] = {}
    for s in servers.values():
        if s.namespace is None:
            continue
        key = (s.domain, s.namespace)
        if key in namespaces:
            raise ConfigError(f"servers.{s.name}: namespace '{s.namespace}' already used by "
                              f"servers.{namespaces[key]} in domain '{s.domain}'")
        namespaces[key] = s.name

    used = {s.domain for s in servers.values()}
    extra = [d for d in dict.fromkeys(r["domain"] for r in recipes.values()) if d not in used]
    data_dir = Path(data_dir or env.get("SCOTTY_GATEWAY_DATA", "/data"))
    return GatewayConfig(member=member, token=token, servers=servers, data_dir=data_dir,
                         log_level=str(raw.get("log_level", "INFO")).upper(), extra_domains=extra)
