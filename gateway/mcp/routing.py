"""Requests for /mcp/<user>[/<integration>] and /api/v1/<user>/...: bearer auth, then the user's runtime.

    /mcp/<user>                             meta tools (meta.py)
    /mcp/<user>/<integration>               the integration's plain tools
    /api/v1/<user>/tools[?integration=]     tool list (debugging, scripts)
    /api/v1/<user>/tool-invocations         {"tool": "<integration>/<tool>", "args": {}} without an LLM
    /api/v1/<user>/status                   per integration: ok / off / error

Tenant isolation lives here: a token opens only its owner's paths. A valid
token for another user gets 403, the same answer whether that user exists or
not.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from ..auth.tokens import Tokens
from ..auth.users import User
from .registry import Registry


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})


def bearer(scope) -> str:
    raw = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
    scheme, _, token = raw.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


class McpRouter:
    """ASGI app for everything below /mcp and /api/v1."""

    def __init__(self, registry: Registry, tokens: Tokens):
        self.registry = registry
        self.tokens = tokens

    async def __call__(self, scope, receive, send):
        parts = scope["path"].split("/")          # ["", "mcp", user, integration?, ...]
        api = parts[1] == "api"
        if api:
            parts = ["", "api"] + parts[3:]       # drop the version segment
        name = parts[2] if len(parts) > 2 else ""
        token = bearer(scope)
        user = self.tokens.authenticate(token) if token else None
        if user is None:
            return await _unauthorized()(scope, receive, send)
        if not name or user.name != name:
            return await JSONResponse({"error": "this token is not valid for this path"},
                                      status_code=403)(scope, receive, send)
        runtime = await self.registry.get(user)
        if api:
            return await self.api(user, runtime, parts[3] if len(parts) > 3 else "", scope, receive, send)
        integration = parts[3] if len(parts) > 3 else ""
        app = runtime.apps.get(integration)
        if app is None:
            known = ", ".join(sorted(k for k in runtime.apps if k)) or "none"
            return await JSONResponse({"error": f"unknown integration '{integration}'; connected: {known}"},
                                      status_code=404)(scope, receive, send)
        return await app(scope, receive, send)

    async def api(self, user: User, runtime, action: str, scope, receive, send):
        request = Request(scope, receive)
        if action == "tools" and request.method == "GET":
            items, errors = await runtime.tool_index(request.query_params.get("integration"))
            response = JSONResponse({"tools": items, "errors": errors})
        elif action == "status" and request.method == "GET":
            body = await runtime.status(fresh=request.query_params.get("fresh") == "1")
            response = JSONResponse(body, status_code=200 if body["ready"] else 503)
        elif action == "tool-invocations" and request.method == "POST":
            try:
                payload = await request.json()
                integration, _, tool = str(payload["tool"]).partition("/")
                args = payload.get("args") or {}
                if not integration or not tool or not isinstance(args, dict):
                    raise ValueError
            except Exception:  # noqa: BLE001
                response = JSONResponse({"error": 'expected {"tool": "<integration>/<tool>", "args": {...}}'},
                                        status_code=400)
            else:
                status, body = await runtime.invoke(integration, tool, args)
                response = JSONResponse(body, status_code=status)
        else:
            response = JSONResponse({"error": "not found"}, status_code=404)
        return await response(scope, receive, send)
