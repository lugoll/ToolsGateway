"""gateway command line.

    gateway serve                         run the gateway (container default)
    gateway check                         validate settings, master key, database and catalog
    gateway keygen                        print a new master key
    gateway rotate-key                    re-encrypt every secret with GATEWAY_MASTER_KEY
                                          (old key in GATEWAY_MASTER_KEY_OLD)
    gateway reset-password USER           set a user's password (prompts; for a locked-out admin)
    gateway tools USER [INTEGRATION]      list a user's tools in-process
    gateway call USER INTEGRATION/TOOL [--args JSON]   call a tool in-process, no HTTP

Settings come from the environment (gateway/settings.py); everything else is
managed in the web UI.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sys

from .settings import Settings, SettingsError


def _logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level, logging.INFO), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # The SDK logs every request at INFO; the audit log already covers tool calls.
    for noisy in ("mcp", "httpx", "httpx2", "fastmcp"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, logging.getLogger().level))
    # caldav warns about every iCalendar quirk it repairs, with the event's text
    # in the message: private data in the log. Call failures are logged by the connector.
    # Imported first: importing caldav.lib.error sets the logger's level itself.
    import caldav.lib.error  # noqa: F401
    logging.getLogger("caldav").setLevel(max(logging.ERROR, logging.getLogger().level))


def _settings() -> Settings:
    try:
        return Settings.from_env()
    except SettingsError as exc:
        sys.exit(f"settings: {exc}")


def _services(settings: Settings):
    from .app import Services
    from .catalog import CatalogError
    from .storage.crypto import CryptoError
    try:
        return Services.open(settings)
    except (CatalogError, CryptoError) as exc:
        sys.exit(str(exc))


def cmd_serve(args) -> int:
    import uvicorn

    from .app import create_app
    settings = _settings()
    _logging(settings.log_level)
    app = create_app(_services(settings))
    # proxy_headers: Caddy in front sets X-Forwarded-For/-Proto; trust it on the
    # addresses in FORWARDED_ALLOW_IPS (uvicorn's default: 127.0.0.1).
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning", access_log=False,
                proxy_headers=True)
    return 0


def cmd_check(args) -> int:
    settings = _settings()
    s = _services(settings)
    print(f"database {settings.db_path}: ok, key {s.secrets.vault.current}")
    print(f"catalog {settings.catalog_dir}: {len(s.catalog.entries)} entries")
    for e in s.catalog:
        print(f"  {e.id:<20} {e.kind:<7} {e.integration:<10} {e.label}")
    users = s.users.all()
    print(f"users: {', '.join(f'{u.name} ({u.role})' for u in users) or 'none yet'}")
    return 0


def cmd_keygen(args) -> int:
    from .storage.crypto import generate_key
    print(generate_key())
    return 0


def cmd_rotate(args) -> int:
    settings = _settings()
    if not settings.master_key_old:
        sys.exit("set GATEWAY_MASTER_KEY_OLD to the previous key and GATEWAY_MASTER_KEY to the new one")
    s = _services(settings)
    print(f"re-encrypted {s.secrets.rotate()} values with key {s.secrets.vault.current}; "
          "GATEWAY_MASTER_KEY_OLD can be removed now")
    return 0


def cmd_reset_password(args) -> int:
    from .auth.passwords import PasswordPolicyError
    s = _services(_settings())
    user = s.users.by_name(args.user)
    if user is None:
        sys.exit(f"no user '{args.user}'")
    password = getpass.getpass(f"new password for {user.name}: ")
    if password != getpass.getpass("again: "):
        sys.exit("the passwords differ")
    try:
        s.users.set_password(user.id, password)
    except PasswordPolicyError as exc:
        sys.exit(str(exc))
    s.audit.record(user.name, "admin", "password_reset", detail={"by": "cli"})
    s.audit.flush()
    print(f"password of {user.name} set; their sessions are closed")
    return 0


def _runtime(s, name: str):
    user = s.users.by_name(name)
    if user is None:
        sys.exit(f"no user '{name}'")
    return s.registry.build(user)


def cmd_tools(args) -> int:
    settings = _settings()
    _logging(settings.log_level)
    s = _services(settings)

    async def run():
        tools, errors = await _runtime(s, args.user).tool_index(args.integration)
        for t in tools:
            print(f"{t['integration']:<12} {t['name']}")
        for integration, err in errors.items():
            print(f"{'!':<12} {integration}: {err}", file=sys.stderr)
        return 1 if errors else 0
    return asyncio.run(run())


def cmd_call(args) -> int:
    settings = _settings()
    _logging(settings.log_level)
    s = _services(settings)
    integration, _, tool = args.target.partition("/")
    try:
        call_args = json.loads(args.args)
    except ValueError as exc:
        sys.exit(f"--args: invalid JSON ({exc})")
    status, body = asyncio.run(_runtime(s, args.user).invoke(integration, tool, call_args))
    s.audit.flush()
    result = body.get("result")
    if result and not result.get("structuredContent"):
        for c in result.get("content", []):
            print(c.get("text", json.dumps(c)))
    else:
        print(json.dumps(result.get("structuredContent") if result else body, indent=2, ensure_ascii=False))
    return 0 if status == 200 and not (result or {}).get("isError") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gateway", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("check")
    sub.add_parser("keygen")
    sub.add_parser("rotate-key")
    r = sub.add_parser("reset-password")
    r.add_argument("user")
    t = sub.add_parser("tools")
    t.add_argument("user")
    t.add_argument("integration", nargs="?")
    c = sub.add_parser("call")
    c.add_argument("user")
    c.add_argument("target", help="INTEGRATION/TOOL")
    c.add_argument("--args", default="{}")
    opts = p.parse_args(sys.argv[1:] if argv is None else argv)
    return {"serve": cmd_serve, "check": cmd_check, "keygen": cmd_keygen, "rotate-key": cmd_rotate,
            "reset-password": cmd_reset_password, "tools": cmd_tools, "call": cmd_call}[opts.command](opts)


if __name__ == "__main__":
    sys.exit(main())
