"""Remote MCP servers, proxied as they are: one FastMCP proxy per connection.

Streamable HTTP only; the gateway runs no child processes. `auth` is an httpx
Auth (the MCP OAuth provider, outbound/oauth/mcp.py) or None when the server
takes static headers or nothing.
"""
from __future__ import annotations

import re

import httpx2
from fastmcp import FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import ProxyClient

from ..catalog import PLACEHOLDER, CatalogEntry


def render_headers(entry: CatalogEntry, values: dict, secrets: dict[str, str]) -> dict[str, str]:
    """Fill {field} placeholders in the catalog's headers. A header whose
    placeholder has no value is left out (e.g. an optional token)."""
    known = {**{k: str(v) for k, v in values.items()}, **secrets}
    out = {}
    for header, template in entry.headers.items():
        names = PLACEHOLDER.findall(template)
        if any(not known.get(n) for n in names):
            continue
        out[header] = re.sub(PLACEHOLDER, lambda m: known[m.group(1)], template)
    return out


def build_proxy(name: str, url: str, headers: dict[str, str], timeout: float,
                auth: httpx2.Auth | None = None) -> FastMCP:
    transport = StreamableHttpTransport(url, headers=headers or None, auth=auth)
    client = ProxyClient(transport, timeout=timeout)
    # "raise": a backend that cannot list its tools is an error here, not an
    # empty list ("warn", FastMCP's default, makes a dead server look healthy
    # with 0 tools).
    return create_proxy(client, name=name, provider_error_strategy="raise")
