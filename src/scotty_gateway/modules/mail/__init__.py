"""Native mail module: IMAP and Gmail-API mailboxes. Read, mark, move, draft; never send or delete."""
from __future__ import annotations

from fastmcp import FastMCP

from .. import ModuleContext
from .config import parse_config
from .oauth import auth
from .tools import build_server


def build(config: dict, ctx: ModuleContext) -> FastMCP:
    return build_server(parse_config(config, ctx.data_dir), ctx)


commands = {"auth": auth}
