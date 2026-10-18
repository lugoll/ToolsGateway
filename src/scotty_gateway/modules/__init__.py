"""Native modules: integrations implemented in this package instead of proxied.

A module is a Python package here that exposes

    build(config: dict, ctx: ModuleContext) -> FastMCP
        the module's tools; `config` is the server's "config" table with ${VAR}
        already substituted (that is where secrets arrive)

and optionally

    commands: dict[str, Callable[[dict, ModuleContext, list[str]], int]]
        extra CLI subcommands, run as `scotty-gateway <command> <server> [args...]`
        (the mail module provides `auth`)

Register it in MODULES below and reference it from a recipe with
`type = "native"`, `module = "<name>"`.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

MODULES = {
    "mail": "scotty_gateway.modules.mail",
}


@dataclass
class ModuleContext:
    server: str          # server name in gateway.json, e.g. "email-lukas"
    member: str          # member id, e.g. "lukas"
    data_dir: Path       # writable, private to this server: <data>/<server>
    log: logging.Logger


def load_module(name: str) -> ModuleType:
    if name not in MODULES:
        raise KeyError(f"unknown native module '{name}' (known: {', '.join(sorted(MODULES))})")
    return importlib.import_module(MODULES[name])
