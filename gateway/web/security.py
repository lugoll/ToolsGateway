"""HTTP hardening of the web UI: security headers and the cross-site request check.

Pages carry no JavaScript at all, so the CSP forbids scripts outright. Every
unsafe request (POST, ...) must come from the gateway's own origin (Origin
header, or Referer when a browser leaves Origin out); together with SameSite
cookies and the per-session form token (common.form) this stops cross-site
form posts, including on the login form. TLS and HSTS are Caddy's job.
"""
from __future__ import annotations

from starlette.responses import PlainTextResponse

CSP = ("default-src 'none'; style-src 'self'; img-src 'self' data:; form-action 'self'; "
       "frame-ancestors 'none'; base-uri 'none'")
HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]
SAFE = {"GET", "HEAD", "OPTIONS"}


class WebSecurity:
    def __init__(self, app, public_origin: str):
        self.app = app
        self.origin = public_origin

    def _same_origin(self, headers: dict[bytes, bytes]) -> bool:
        origin = headers.get(b"origin", b"").decode("latin-1")
        if origin:
            return origin == self.origin
        referer = headers.get(b"referer", b"").decode("latin-1")
        return referer == self.origin or referer.startswith(self.origin + "/")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        if scope["method"] not in SAFE and not self._same_origin(headers):
            response = PlainTextResponse("cross-site request refused (check GATEWAY_PUBLIC_URL)", status_code=403)
            return await self._send_with_headers(response, scope, receive, send)
        return await self._send_with_headers(self.app, scope, receive, send)

    async def _send_with_headers(self, app, scope, receive, send):
        static = scope["path"].startswith("/static/")

        async def wrapped(message):
            if message["type"] == "http.response.start":
                extra = list(HEADERS)
                if not static:
                    extra.append((b"cache-control", b"no-store"))
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)
        return await app(scope, receive, wrapped)
