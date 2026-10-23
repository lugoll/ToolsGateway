"""The web UI through Starlette's TestClient: login, sessions, CSRF, roles, connections, tokens."""
import re

import pytest
from starlette.testclient import TestClient

import fake_remote
from gateway.app import create_app

ORIGIN = "https://tools.test"          # settings.public_url in conftest


@pytest.fixture
def app(services):
    return create_app(services)


def client(app) -> TestClient:
    return TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}, follow_redirects=False)


def login(c: TestClient, name: str, password: str):
    return c.post("/login", data={"name": name, "password": password, "next": "/"})


def csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@pytest.fixture
def admin(app):
    with client(app) as c:                   # entering runs the lifespan: bootstrap admin
        assert login(c, "admin", "admin-password-1").status_code == 303
        yield c


def test_login_cookie_and_headers(app):
    with client(app) as c:
        r = c.get("/")
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        r = c.get("/login")
        assert "default-src 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
        assert login(c, "admin", "wrong-password").status_code == 401
        r = login(c, "admin", "admin-password-1")
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("__Host-session=") and "HttpOnly" in cookie and "Secure" in cookie
        assert "samesite=strict" in cookie.lower() and "Path=/" in cookie
        assert c.get("/").status_code == 200


def test_login_throttling(app):
    with client(app) as c:
        for _ in range(5):
            assert login(c, "admin", "wrong-password").status_code == 401
        assert login(c, "admin", "wrong-password").status_code == 401     # 6th: now blocked for 1 s
        assert login(c, "admin", "admin-password-1").status_code == 429


def test_csrf_and_cross_site(admin):
    assert admin.post("/tokens", data={"action": "create", "label": "x"}).status_code == 403      # no form token
    page = admin.get("/tokens").text
    r = admin.post("/tokens", data={"action": "create", "label": "x", "csrf": csrf(page)},
                   headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and "cross-site" in r.text
    r = admin.post("/tokens", data={"action": "create", "label": "x", "csrf": csrf(page)})
    assert r.status_code == 200 and "shown only once" in r.text


def test_admin_creates_user_who_sets_up_mail_and_token(app, admin, services):
    page = admin.get("/admin/users").text
    r = admin.post("/admin/users", data={"name": "masha", "display_name": "Masha", "password": "masha-password-1",
                                         "csrf": csrf(page)})
    assert r.status_code == 303
    with client(app) as m:
        login(m, "masha", "masha-password-1")
        assert m.get("/admin/users").status_code == 403                     # not an admin
        form = m.get("/connections/new/icloud-mail").text
        r = m.post("/connections/new/icloud-mail", data={
            "csrf": csrf(form), "name": "icloud", "label": "iCloud", "email": "m@icloud.com",
            "password": "app-specific-pw"})
        assert r.status_code == 303
        detail_url = r.headers["location"]
        detail = m.get(detail_url).text
        assert "app-specific-pw" not in detail and "set " in detail             # write-only
        # Capabilities: drop move.
        r = m.post(f"{detail_url}/capabilities", data={"csrf": csrf(detail), "cap_read": "on", "cap_draft": "on"})
        c = services.connections.for_user(services.users.by_name("masha").id)[0]
        assert services.connections.effective_capabilities(c) == {"read", "draft"}
        # A token, shown once, opens masha's API only.
        tokens = m.get("/tokens").text
        r = m.post("/tokens", data={"csrf": csrf(tokens), "action": "create", "label": "laptop"})
        token = re.search(r'value="(tgw_[^"]+)"', r.text).group(1)
        assert token not in m.get("/tokens").text
        api = m.get("/api/v1/masha/status", headers={"Authorization": f"Bearer {token}"})
        assert api.json()["integrations"]["mail"]["accounts"] == ["icloud"]
        assert m.get("/api/v1/admin/status", headers={"Authorization": f"Bearer {token}"}).status_code == 403
        # The audit page shows masha's own entries only.
        activity = m.get("/audit").text
        assert "connection_created" in activity and "login" in activity

    # The admin may replace masha's secret, but not change anything else.
    detail = admin.get(detail_url).text
    assert "belongs to Masha" in detail
    r = admin.post(f"{detail_url}/secret", data={"csrf": csrf(detail), "name": "password", "value": "new-pw"})
    assert r.status_code == 303 and services.secrets.get(c.id, "password") == "new-pw"
    assert admin.post(f"{detail_url}/delete", data={"csrf": csrf(detail), "confirm": "icloud"}).status_code == 403
    overwritten = services.audit.query(user="masha", kind="admin")[0]
    assert overwritten.action == "secret_overwritten_by_admin" and overwritten.detail["by"] == "admin"


def test_remote_tool_toggles(app, admin, services, serve):
    url = serve(fake_remote.app()) + "/mcp"
    form = admin.get("/connections/new/fake-remote").text
    r = admin.post("/connections/new/fake-remote", data={"csrf": csrf(form), "name": "tasks", "label": "Tasks",
                                                          "url": url, "token": "t"})
    detail_url = r.headers["location"]
    detail = admin.get(detail_url).text
    assert 'name="tool_hello" aria-label="hello" checked' in detail
    assert 'name="tool_delete_task" aria-label="delete_task" >' in detail.replace("  ", " ")
    assert "never served" in detail                                          # send_message is blocked
    listed = re.search(r'name="listed" value="([^"]+)"', detail).group(1)
    admin.post(f"{detail_url}/tools", data={"csrf": csrf(detail), "listed": listed,
                                             "tool_delete_task": "on", "tool_send_message": "on"})
    c = services.connections.for_user(services.users.by_name("admin").id)[0]
    assert c.tools == {"enabled": ["delete_task"], "disabled": ["hello"]}
    r = admin.post(f"{detail_url}/test", data={"csrf": csrf(detail)})
    assert "Works: connected, 1 tools served" in admin.get(detail_url).text


def test_password_change_ends_sessions(app, admin):
    page = admin.get("/account").text
    r = admin.post("/account", data={"csrf": csrf(page), "current": "admin-password-1",
                                     "new": "another-password-2", "again": "another-password-2"})
    assert r.status_code == 303
    assert admin.get("/").status_code == 303                                # logged out
    with client(app) as c:
        assert login(c, "admin", "another-password-2").status_code == 303
