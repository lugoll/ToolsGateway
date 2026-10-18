"""A stdio MCP server for the proxy tests. Logs its pid so tests can count spawns."""
import os

from fastmcp import FastMCP

with open(os.environ["SPAWN_LOG"], "a") as fh:
    fh.write(f"{os.getpid()}\n")

mcp = FastMCP("fake")


@mcp.tool
def hello(name: str) -> str:
    return f"hi {name} secret={os.environ.get('FAKE_SECRET')} leaked={os.environ.get('GATEWAY_LEAK')}"


@mcp.tool
def send_email(to: str) -> str:
    return "SENT"


@mcp.tool
def crash() -> str:
    os._exit(3)


mcp.run(show_banner=False)
