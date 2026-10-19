"""scotty-gateway command line.

    scotty-gateway serve                      run the gateway (container default)
    scotty-gateway check-config               validate the member file, show what would run
    scotty-gateway tools [--domain D]         list tools in-process (starts the backends)
    scotty-gateway call SERVER/TOOL [--args JSON]   call a tool in-process, no HTTP
    scotty-gateway <command> SERVER [...]     a native module's own command (mail: auth)

The member file comes from --config or SCOTTY_GATEWAY_CONFIG (compose sets
/etc/scotty-gateway/members/<member>.toml), the recipes from
SCOTTY_GATEWAY_CONNECTORS (default /etc/scotty-gateway/connectors).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from . import audit
from .config import ConfigError, GatewayConfig, load
from .modules import ModuleContext, load_module

DEFAULT_CONFIG = "/etc/scotty-gateway/member.toml"


def _load(path: str) -> GatewayConfig:
    try:
        return load(path)
    except ConfigError as exc:
        sys.exit(f"config error: {exc}")


def _logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level, logging.INFO), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # The SDK logs every request at INFO; the audit log already covers tool calls.
    for noisy in ("mcp", "httpx", "fastmcp"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, logging.getLogger().level))


def cmd_serve(cfg: GatewayConfig, args) -> int:
    import uvicorn

    from .app import Gateway
    # HOME holds the npm/uv caches of the stdio children (image: /data/home).
    os.makedirs(os.environ.get("HOME", "/tmp"), exist_ok=True)
    audit.setup(cfg.data_dir)
    app = Gateway(cfg).asgi()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)
    return 0


def cmd_check(cfg: GatewayConfig, _args) -> int:
    print(f"member {cfg.member}: {len(cfg.servers)} servers, domains {', '.join(cfg.domains())}")
    for s in cfg.servers.values():
        target = {"stdio": f"{s.command} {' '.join(s.args)}", "http": s.url,
                  "native": f"module {s.module}"}[s.type]
        status = "runs" if s.enabled else s.reason
        extra = "".join([f" deny={s.deny}" if s.deny else "", f" allow={s.allow}" if s.allow else "",
                         f" namespace={s.namespace}" if s.namespace else ""])
        print(f"  {s.name:<24} {s.domain:<10} {s.type:<6} {target}{extra}\n  {'':<24} -> {status}")
    return 0


def cmd_tools(cfg: GatewayConfig, args) -> int:
    from .app import Gateway

    async def run():
        tools, errors = await Gateway(cfg).tool_index(args.domain)
        for t in tools:
            print(f"{t['domain']:<10} {t['server']:<24} {t['name']}")
        for server, err in errors.items():
            print(f"{'!':<10} {server:<24} {err}", file=sys.stderr)
        return 1 if errors else 0
    return asyncio.run(run())


def cmd_call(cfg: GatewayConfig, args) -> int:
    from .app import Gateway
    server, _, tool = args.target.partition("/")
    try:
        call_args = json.loads(args.args)
    except ValueError as exc:
        sys.exit(f"--args: invalid JSON ({exc})")
    audit.setup(cfg.data_dir)
    status, body = asyncio.run(Gateway(cfg).invoke(server, tool, call_args))
    result = body.get("result")
    if result and not result.get("structuredContent"):
        for c in result.get("content", []):
            print(c.get("text", json.dumps(c)))
    else:
        print(json.dumps(result.get("structuredContent") if result else body, indent=2, ensure_ascii=False))
    return 0 if status == 200 and not (result or {}).get("isError") else 1


def cmd_module(cfg: GatewayConfig, command: str, rest: list[str]) -> int:
    if not rest:
        sys.exit(f"usage: scotty-gateway {command} SERVER [args...]")
    srv = cfg.servers.get(rest[0])
    if srv is None or srv.type != "native":
        sys.exit(f"{rest[0]}: not a native server in this gateway "
                 f"(native: {', '.join(n for n, s in cfg.servers.items() if s.type == 'native')})")
    module = load_module(srv.module)
    handler = getattr(module, "commands", {}).get(command)
    if handler is None:
        sys.exit(f"module '{srv.module}' has no command '{command}'")
    data_dir = cfg.data_dir / srv.name
    data_dir.mkdir(parents=True, exist_ok=True)
    ctx = ModuleContext(server=srv.name, member=cfg.member, data_dir=data_dir,
                        log=logging.getLogger(f"scotty_gateway.{srv.name}"))
    return int(handler(srv.config, ctx, rest[1:]) or 0)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="scotty-gateway", description=__doc__.split("\n\n")[0])
    p.add_argument("--config", default=os.environ.get("SCOTTY_GATEWAY_CONFIG", DEFAULT_CONFIG))
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=3050)
    sub.add_parser("check-config")
    t = sub.add_parser("tools")
    t.add_argument("--domain")
    c = sub.add_parser("call")
    c.add_argument("target", help="SERVER/TOOL")
    c.add_argument("--args", default="{}")

    known = {"serve", "check-config", "tools", "call"}
    first, i = None, 0
    while i < len(argv):
        if argv[i] == "--config":
            i += 2
            continue
        if not argv[i].startswith("-"):
            first = i
            break
        i += 1
    if first is not None and argv[first] not in known:
        # Module command: everything after it belongs to the module.
        pre, first, rest = argv[:first], argv[first], argv[first + 1:]
        opts = p.parse_args(pre + ["check-config"])
        cfg = _load(opts.config)
        _logging(cfg.log_level)
        return cmd_module(cfg, first, rest)

    opts = p.parse_args(argv)
    cfg = _load(opts.config)
    _logging(cfg.log_level)
    return {"serve": cmd_serve, "check-config": cmd_check, "tools": cmd_tools,
            "call": cmd_call}[opts.command](cfg, opts)


if __name__ == "__main__":
    sys.exit(main())
