"""A tiny MCP server for the tests: launched over stdio, like a real one.

It uses the SDK's own server side, so the tests exercise the real protocol
(the handshake, listing tools, calling them, cancelling) without a network.
"""

import os

import anyio
from mcp.server import MCPServer

server = MCPServer("demo")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@server.tool()
def fail() -> str:
    """Always fails."""
    raise ValueError("this tool always fails")


@server.tool()
async def slow(seconds: float) -> str:
    """Wait, then say so."""
    await anyio.sleep(seconds)
    return "done"


@server.tool()
def pid() -> int:
    """This server's process id, so a test can check it has stopped."""
    return os.getpid()


if __name__ == "__main__":
    server.run()
