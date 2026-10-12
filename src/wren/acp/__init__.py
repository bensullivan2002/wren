"""ACP mode: Wren driven by another program (an editor, or a terminal UI
such as Toad) over the Agent Client Protocol, where the REPL is Wren driven
by a person at a keyboard.

    jsonrpc.py   the wire: JSON-RPC messages, one per line, in both directions
    server.py    the translation between ACP's messages and Wren's agent
"""

from __future__ import annotations

import sys

from wren.acp.jsonrpc import Connection
from wren.acp.server import AcpServer, AgentFactory


def run_acp(make_agent: AgentFactory) -> None:
    """Speak ACP on stdin and stdout until the client hangs up."""
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8")  # the protocol is UTF-8, whatever the terminal's locale says

    # In this mode stdout carries the protocol and nothing else. One stray
    # print() anywhere (ours, or a library's) would land in the middle of the
    # message stream and break the client. So we keep the real stdout for the
    # connection, and point `sys.stdout` at stderr, where a client shows or
    # logs whatever arrives.
    protocol_out, sys.stdout = sys.stdout, sys.stderr
    AcpServer(Connection(sys.stdin, protocol_out), make_agent).serve()


__all__ = ["AcpServer", "Connection", "run_acp"]
