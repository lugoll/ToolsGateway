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


def write_config(tmp_path: Path, servers: dict, member: str = "tester") -> Path:
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps({"member": member, "auth": {"token": "${GW_TOKEN}"}, "servers": servers}))
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
