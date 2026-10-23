"""Upstream OAuth: Google's paste-back flow (fake Google) and MCP OAuth end to end (FastMCP's in-memory server)."""
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastmcp import FastMCP
from fastmcp.server.auth.auth import ClientRegistrationOptions
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider

from conftest import free_port
from gateway.app import Services, create_app
from gateway.outbound.oauth import google as google_oauth
from test_web import client, csrf, login

GMAIL = "https://www.googleapis.com/auth/gmail.modify"


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode()
        self.ok = status < 400

    def json(self):
        return self._body


@pytest.fixture
def fake_google(monkeypatch):
    calls = {"scope": GMAIL, "email": "l@gmail.com", "posts": []}

    def post(url, data=None, timeout=None):
        calls["posts"].append(data)
        return FakeResp(200, {"access_token": "at", "refresh_token": "rt-1", "scope": calls["scope"],
                              "expires_in": 3600})

    def get(url, headers=None, timeout=None):
        assert headers == {"Authorization": "Bearer at"}
        return FakeResp(200, {"emailAddress": calls["email"]})

    monkeypatch.setattr(google_oauth.requests, "post", post)
    monkeypatch.setattr(google_oauth.requests, "get", get)
    return calls


def test_google_paste_back(services, fake_google):
    app = create_app(services)
    with client(app) as c:
        login(c, "admin", "admin-password-1")
        user = services.users.by_name("admin")
        conn = services.connections.create(user.id, services.catalog.get("gmail"), "gmail", "Gmail",
                                           {"email": "l@gmail.com"}, {}, by="admin")
        r = c.get(f"/oauth/google/start/{conn.id}")
        assert r.status_code == 303 and "OAuth client" in c.get(r.headers["location"]).text   # not set up yet
        services.secrets.set_instance("google_client_id", "cid.apps.googleusercontent.com", by="admin")
        services.secrets.set_instance("google_client_secret", "csecret", by="admin")

        def attempt():
            page = c.get(f"/oauth/google/start/{conn.id}").text
            url = re.search(r'href="(https://accounts.google.com[^"]+)"', page).group(1).replace("&amp;", "&")
            q = parse_qs(urlsplit(url).query)
            assert q["redirect_uri"] == ["http://localhost"] and q["code_challenge_method"] == ["S256"]
            assert q["scope"] == [GMAIL] and q["login_hint"] == ["l@gmail.com"] and q["access_type"] == ["offline"]
            pasted = f"http://localhost/?state={q['state'][0]}&code=4/abc&scope={GMAIL}"
            r = c.post("/oauth/google/finish", data={"csrf": csrf(page), "pasted": pasted})
            return c.get(r.headers["location"]).text

        fake_google["scope"] = f"{GMAIL} https://mail.google.com/"
        assert "expected exactly" in attempt()                                   # broader scope refused
        fake_google["scope"], fake_google["email"] = GMAIL, "other@gmail.com"
        assert "signed in as other@gmail.com" in attempt()
        fake_google["email"] = "l@gmail.com"
        page = attempt()
        assert "Connected." in page
        assert fake_google["posts"][-1]["code_verifier"] and fake_google["posts"][-1]["client_secret"] == "csecret"
        stored = json.loads(services.secrets.get(conn.id, "oauth_token"))
        assert stored == {"refresh_token": "rt-1", "scopes": [GMAIL], "email": "l@gmail.com"}
        # The runtime combines the refresh token with the instance client.
        backend_token = json.loads(services.registry._google_token(services.secrets.get(conn.id, "oauth_token")))
        assert backend_token["client_id"] == "cid.apps.googleusercontent.com" and backend_token["refresh_token"] == "rt-1"
        # A replayed state does nothing.
        r = c.post("/oauth/google/finish", data={"csrf": csrf(page), "pasted": "http://localhost/?state=x&code=y"})
        assert "does not belong" in c.get(r.headers["location"]).text


@pytest.fixture
def oauth_server(serve):
    """Server and authorization server under /auth, as with Todoist (token endpoint
    https://todoist.com/oauth/access_token): nothing answers at <origin>/token, so
    a refresh that guesses the endpoint instead of using the discovered one fails."""
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    base = f"{origin}/auth"
    provider = InMemoryOAuthProvider(base_url=base,
                                     client_registration_options=ClientRegistrationOptions(enabled=True))
    mcp = FastMCP("secured", auth=provider)

    @mcp.tool
    def secret_hello() -> str:
        return "hello from behind OAuth"

    inner = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

    async def under_auth(scope, receive, send):
        # FastMCP serves its routes at the root; publish them under /auth only.
        if scope["type"] == "http":
            path = scope["path"]
            if path == "/.well-known/oauth-authorization-server/auth":
                path = "/.well-known/oauth-authorization-server"
            elif path.startswith("/auth/"):
                path = path.removeprefix("/auth")
            elif not path.startswith("/.well-known/oauth-protected-resource/"):
                await send({"type": "http.response.start", "status": 404, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
            scope = {**scope, "path": path, "raw_path": path.encode()}
        await inner(scope, receive, send)

    serve(under_auth, port=port)
    return base, provider


def test_mcp_oauth_end_to_end(settings, oauth_server):
    base, provider = oauth_server
    services = Services.open(settings)
    app = create_app(services)
    with client(app) as c:
        login(c, "admin", "admin-password-1")
        user = services.users.by_name("admin")
        conn = services.connections.create(user.id, services.catalog.get("fake-oauth"), "secured", "Secured",
                                           {"url": f"{base}/mcp"}, {}, by="admin")
        token, _ = services.tokens.create(user.id, "test")
        api = {"Authorization": f"Bearer {token}"}
        status = c.get("/api/v1/admin/status", headers=api).json()
        assert "not authorised yet" in status["integrations"]["secured"]["reason"]

        r = c.get(f"/oauth/mcp/start/{conn.id}")
        assert r.status_code == 303 and r.headers["location"].startswith(f"{base}/authorize?")
        # The (auto-approving) authorization server sends the browser back to the gateway.
        back = httpx.get(r.headers["location"], follow_redirects=False)
        callback = back.headers["location"]
        assert callback.startswith("https://tools.test/oauth/mcp/callback?")
        c.cookies.clear()                                     # no session cookie, as after a cross-site redirect
        done = c.get(callback.removeprefix("https://tools.test"))
        assert done.status_code == 200 and "Connected, 1 tools available." in done.text
        assert services.secrets.get(conn.id, "oauth_client") and services.secrets.get(conn.id, "oauth_token")
        server = json.loads(services.secrets.get(conn.id, "oauth_token"))["server"]
        assert server["oauth_metadata"]["token_endpoint"] == f"{base}/token"

        r = c.post("/api/v1/admin/tool-invocations", headers=api, json={"tool": "secured/secret_hello"})
        assert r.json()["result"]["content"][0]["text"] == "hello from behind OAuth"
        assert c.get(callback.removeprefix("https://tools.test")).status_code == 400       # state is single-use

    # After a restart, with the access token expired: the provider refreshes instead of asking again.
    stored = json.loads(services.secrets.get(conn.id, "oauth_token"))
    stored["expires_at"] = time.time() - 10
    services.secrets.set(conn.id, "oauth_token", json.dumps(stored), "test")
    services.db.close()
    services = Services.open(settings)
    app = create_app(services)
    with client(app) as c:
        r = c.post("/api/v1/admin/tool-invocations", headers=api, json={"tool": "secured/secret_hello"})
        assert r.status_code == 200 and r.json()["result"]["content"][0]["text"] == "hello from behind OAuth"
    refreshed = json.loads(services.secrets.get(conn.id, "oauth_token"))
    assert refreshed["access_token"] != stored["access_token"] and refreshed["expires_at"] > time.time()
    services.db.close()
