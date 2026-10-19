import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

HERE = Path(__file__).parent
TOKEN = "t" * 32


def toml_value(v) -> str:
    """Enough TOML for test recipes: strings, numbers, bools, lists, inline tables."""
    if isinstance(v, dict):
        return "{" + ", ".join(f"{json.dumps(k)} = {toml_value(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return json.dumps(v)


def write_recipe(cdir: Path, rid: str, domain: str, server: dict, accounts: dict | None = None) -> None:
    lines = [f'name = "{rid}"', f'domain = "{domain}"', "[server]"]
    lines += [f"{k} = {toml_value(v)}" for k, v in server.items()]
    for acc, table in (accounts or {}).items():
        lines.append(f"[accounts.{acc}]")
        lines += [f"{k} = {toml_value(v)}" for k, v in table.items()]
    (cdir / rid).mkdir(parents=True, exist_ok=True)
    (cdir / rid / "recipe.toml").write_text("\n".join(lines) + "\n")


def write_config(tmp_path: Path, servers: dict, member: str = "tester", entries: list | None = None) -> Path:
    """One recipe per server (key "<recipe>-<member>", `disabled`/`note` go to the
    member's entry), the member file listing them, SCOTTY_GATEWAY_CONNECTORS set
    by the `env` fixture. Returns the member file."""
    cdir = tmp_path / "connectors"
    cdir.mkdir(exist_ok=True)
    out = [f'member = "{member}"', '[auth]', 'token = "${GW_TOKEN}"']
    for name, spec in servers.items():
        rid = name.removesuffix(f"-{member}")
        spec = dict(spec)
        entry = {"recipe": rid, **{k: spec.pop(k) for k in ("disabled", "note") if k in spec}}
        write_recipe(cdir, rid, spec.pop("domain"), spec)
        out += ["[[connectors]]"] + [f"{k} = {toml_value(v)}" for k, v in entry.items()]
    for entry in entries or []:
        out += ["[[connectors]]"] + [f"{k} = {toml_value(v)}" for k, v in entry.items()]
    path = tmp_path / f"{member}.toml"
    path.write_text("\n".join(out) + "\n")
    return path


def fake_stdio(tmp_path: Path, **extra) -> dict:
    return {"domain": "tasks", "type": "stdio", "command": sys.executable,
            "args": [str(HERE / "fake_backend.py")],
            "env": {"SPAWN_LOG": str(tmp_path / "spawn.log"), "FAKE_SECRET": "${FAKE_SECRET}"}, **extra}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("GW_TOKEN", TOKEN)
    monkeypatch.setenv("FAKE_SECRET", "s3cret")
    monkeypatch.setenv("GATEWAY_LEAK", "must-not-reach-children")
    monkeypatch.setenv("SCOTTY_GATEWAY_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("SCOTTY_GATEWAY_CONNECTORS", str(tmp_path / "connectors"))
    (tmp_path / "connectors").mkdir()
    return tmp_path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def serve():
    """Run an ASGI app on a free port in a thread; yields the base URL."""
    servers = []

    def start(app):
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        servers.append((server, thread))
        return f"http://127.0.0.1:{port}"

    yield start
    for server, thread in servers:
        server.should_exit = True
        thread.join(timeout=10)
