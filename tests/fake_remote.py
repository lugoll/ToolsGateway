"""A remote MCP server for the proxy tests (streamable HTTP, served by the `serve` fixture)."""
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

mcp = FastMCP("fake-remote")


@mcp.tool
def hello(name: str) -> str:
    """Say hello to someone."""
    auth = get_http_headers(include={"authorization"}).get("authorization", "")
    return f"hi {name} auth={auth}"


@mcp.tool
def delete_task(id: str) -> str:
    """Delete a task (off by default in the catalog)."""
    return f"deleted {id}"


@mcp.tool
def send_message(to: str) -> str:
    """Forbidden by the catalog: never served."""
    return "SENT"


def app():
    return mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
