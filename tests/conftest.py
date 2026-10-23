import shutil
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from gateway.app import Services
from gateway.settings import REPO_CATALOG, Settings, parse_key
from gateway.storage.crypto import generate_key

HERE = Path(__file__).parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def serve():
    """Run an ASGI app on a free port in a thread; yields a function returning the base URL."""
    servers = []

    def start(app, port: int | None = None):
        port = port or free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                               lifespan="on"))
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


@pytest.fixture
def catalog_dir(tmp_path) -> Path:
    """The repo catalog plus test-only entries from tests/catalog."""
    target = tmp_path / "catalog"
    shutil.copytree(REPO_CATALOG, target)
    for extra in (HERE / "catalog").glob("*.yaml"):
        shutil.copy(extra, target / extra.name)
    return target


@pytest.fixture
def settings(tmp_path, catalog_dir) -> Settings:
    return Settings(master_key=parse_key(generate_key(), "test"), master_key_old=None,
                    data_dir=tmp_path / "data", catalog_dir=catalog_dir, public_url="https://tools.test",
                    admin_user="admin", admin_password="admin-password-1")


@pytest.fixture
def services(settings) -> Services:
    s = Services.open(settings)
    yield s
    s.db.close()
