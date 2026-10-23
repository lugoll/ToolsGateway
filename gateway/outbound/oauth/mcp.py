"""OAuth for remote MCP servers (MCP authorization spec), on the MCP SDK's client provider.

The SDK does discovery, dynamic client registration, PKCE, resource indicators
and refresh; this module gives it storage (the connection's encrypted secrets
`oauth_token` and `oauth_client`) and runs its interactive step through the web
UI:

  "Connect" (web)   start(): the provider runs in a background task; its
                    redirect handler hands the authorization URL to the browser
  /oauth/mcp/callback  finish(): delivers code and state to the waiting task,
                    which exchanges them and stores the tokens. The browser
                    arrives from another site, so the SameSite=Strict session
                    cookie is not sent: the one-time, unguessable `state`
                    (bound to the user who started) identifies the flow.

At runtime (registry) the provider has no interactive step: it uses and
refreshes the stored tokens, and a server that wants a new sign-in fails the
call with "connect it again in the web UI". The SDK discovers the
authorization server only on a 401, so the stored token also keeps the
discovered metadata: after a restart an expired token is refreshed at the
server's real token endpoint, not at a guessed <origin>/token.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.client.auth import OAuthClientProvider, OAuthFlowError
from mcp.shared.auth import (AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthMetadata,
                             OAuthToken, ProtectedResourceMetadata)

log = logging.getLogger(__name__)

START_TIMEOUT = 30.0
PENDING_TTL = 10 * 60.0
FINISH_TIMEOUT = 60.0


class McpAuthError(RuntimeError):
    """Shown to the user as is."""


class SecretTokenStorage:
    """TokenStorage over a connection's secrets. Keeps the absolute expiry and
    the discovered server metadata (`server`) next to the token, which the
    SDK's OAuthToken (expires_in) does not."""

    def __init__(self, secrets_store, connection_id: int):
        self.secrets = secrets_store
        self.cid = connection_id
        self.server: dict | None = None

    def _stored(self) -> dict:
        raw = self.secrets.get(self.cid, "oauth_token")
        return json.loads(raw) if raw else {}

    def expires_at(self) -> float | None:
        return self._stored().get("expires_at")

    async def get_tokens(self) -> OAuthToken | None:
        data = self._stored()
        if not data:
            return None
        data.pop("expires_at", None)
        self.server = data.pop("server", None)
        return OAuthToken.model_validate(data)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = tokens.model_dump(mode="json", exclude_none=True)
        if tokens.expires_in:
            data["expires_at"] = time.time() + tokens.expires_in
        if self.server:
            data["server"] = self.server
        self.secrets.set(self.cid, "oauth_token", json.dumps(data), "oauth")

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self.secrets.get(self.cid, "oauth_client")
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.secrets.set(self.cid, "oauth_client", client_info.model_dump_json(exclude_none=True), "oauth")


class GatewayOAuthProvider(OAuthClientProvider):
    """Restores the stored token's expiry and server metadata when loading it,
    so an expired access token is refreshed (at the right endpoint) instead of
    triggering a whole new authorization."""

    async def _initialize(self) -> None:
        await super()._initialize()
        storage = self.context.storage
        if not isinstance(storage, SecretTokenStorage):
            return
        self.context.token_expiry_time = storage.expires_at()
        server = storage.server or {}
        if server.get("oauth_metadata"):
            self.context.oauth_metadata = OAuthMetadata.model_validate(server["oauth_metadata"])
        if server.get("resource_metadata"):
            self.context.protected_resource_metadata = ProtectedResourceMetadata.model_validate(
                server["resource_metadata"])
        self.context.auth_server_url = server.get("auth_server_url") or self.context.auth_server_url

    async def _handle_token_response(self, response) -> None:
        # A new sign-in: the metadata discovered for it goes into the stored token.
        storage = self.context.storage
        if isinstance(storage, SecretTokenStorage) and self.context.oauth_metadata:
            prm = self.context.protected_resource_metadata
            storage.server = {"auth_server_url": self.context.auth_server_url,
                              "oauth_metadata": self.context.oauth_metadata.model_dump(mode="json", exclude_none=True),
                              "resource_metadata": prm.model_dump(mode="json", exclude_none=True) if prm else None}
        await super()._handle_token_response(response)


@dataclass
class Pending:
    user_id: int
    connection_id: int
    code: asyncio.Future
    task: asyncio.Task
    created: float = field(default_factory=time.monotonic)


class McpOAuth:
    def __init__(self, secrets_store, public_url: str):
        self.secrets = secrets_store
        self.redirect_uri = f"{public_url}/oauth/mcp/callback"
        self._pending: dict[str, Pending] = {}

    def metadata(self, scopes: list[str]) -> OAuthClientMetadata:
        return OAuthClientMetadata(client_name="Tools Gateway", redirect_uris=[self.redirect_uri],
                                   grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                                   application_type="web", scope=" ".join(scopes) or None)

    def provider(self, url: str, connection_id: int, scopes: list[str], redirect_handler=None,
                 callback_handler=None) -> GatewayOAuthProvider:
        async def no_interaction(*_args) -> None:
            raise OAuthFlowError("the server wants a new sign-in: connect it again in the gateway's web UI")
        return GatewayOAuthProvider(url, self.metadata(scopes), SecretTokenStorage(self.secrets, connection_id),
                                    redirect_handler=redirect_handler or no_interaction,
                                    callback_handler=callback_handler or no_interaction)

    def runtime_auth(self, url: str, connection_id: int, scopes: list[str]) -> GatewayOAuthProvider | None:
        """The provider for the registry, or None while the connection was never authorised."""
        if self.secrets.get(connection_id, "oauth_token") is None:
            return None
        return self.provider(url, connection_id, scopes)

    def _expire(self) -> None:
        now = time.monotonic()
        for state, p in list(self._pending.items()):
            if now - p.created > PENDING_TTL:
                p.task.cancel()
                del self._pending[state]

    async def start(self, user_id: int, connection_id: int, url: str, headers: dict, scopes: list[str]) -> str | None:
        """Begin the sign-in. Returns the URL to send the browser to, or None when the
        stored tokens still work and nothing is needed."""
        self._expire()
        loop = asyncio.get_running_loop()
        redirect: asyncio.Future = loop.create_future()
        code: asyncio.Future = loop.create_future()

        async def on_redirect(auth_url: str) -> None:
            if not redirect.done():
                redirect.set_result(auth_url)

        async def on_callback() -> AuthorizationCodeResult:
            return await asyncio.wait_for(code, PENDING_TTL)

        provider = self.provider(url, connection_id, scopes, on_redirect, on_callback)

        async def connect() -> int:
            transport = StreamableHttpTransport(url, headers=headers or None, auth=provider)
            async with Client(transport, timeout=FINISH_TIMEOUT) as client:
                return len(await client.list_tools())

        task = asyncio.create_task(connect(), name=f"mcp-oauth-{connection_id}")
        done, _ = await asyncio.wait({redirect, task}, timeout=START_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
        if redirect in done:
            auth_url = redirect.result()
            state = (parse_qs(urlsplit(auth_url).query).get("state") or [""])[0]
            self._pending[state] = Pending(user_id, connection_id, code, task)
            return auth_url
        if task in done:
            exc = task.exception()
            if exc is None:
                return None                              # already authorised
            raise McpAuthError(f"cannot start the sign-in: {type(exc).__name__}: {exc}")
        task.cancel()
        raise McpAuthError("the server did not answer in time")

    async def finish(self, query: dict[str, str]) -> tuple[int, int, int]:
        """Hand the callback to the waiting flow; returns (user id, connection id, tools served)."""
        state = query.get("state", "")
        pending = self._pending.pop(state, None) if state else None
        if pending is None:
            raise McpAuthError("this sign-in is unknown or expired; start again from the connection page")
        if query.get("error"):
            pending.task.cancel()
            raise McpAuthError(f"the server refused: {query.get('error_description') or query['error']}")
        pending.code.set_result(AuthorizationCodeResult(code=query.get("code", ""), state=state,
                                                        iss=query.get("iss")))
        try:
            n = await asyncio.wait_for(pending.task, FINISH_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            raise McpAuthError(f"sign-in failed: {type(exc).__name__}: {exc}") from None
        return pending.user_id, pending.connection_id, n
