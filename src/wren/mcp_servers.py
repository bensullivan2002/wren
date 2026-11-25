"""Running MCP servers: starting them, calling their tools, and stopping them.

The official MCP SDK is asynchronous: every call is `await client.call_tool(...)`
inside an event loop. Wren's core is synchronous (D34): the agent loop calls
`registry.run()` and waits for the answer. Rewriting Wren around `async`
just to talk to MCP servers would touch every part of it, so instead the SDK
gets an event loop of its own, on a background thread (D138):

    Wren's thread                         the MCP thread (an event loop)
    ─────────────                         ──────────────────────────────
    McpServers.call("github", ...)  ───►  await client.call_tool(...)
       waits on a Future            ◄───  the result, or an exception

anyio, the async library the SDK is built on, provides exactly this as a
"blocking portal": `portal.start_task_soon(coroutine_function, ...)` runs a
coroutine on the portal's loop and hands back an ordinary
`concurrent.futures.Future` the calling thread can wait on.

One rule shapes the rest of this file. An MCP client is an async context
manager (`async with Client(...)`), and anyio insists it is opened and closed
by the SAME task: it owns a task group, and task groups are scoped to the
task that entered them. So each server gets one long-lived task,
`_serve()`, that opens the client, reports that it's ready, then sleeps
until Wren exits, when it's cancelled and the `async with` closes the client
(which closes the server's stdin, waits, and then terminates the process).
Calls to its tools run as separate short tasks, which the SDK allows.

All servers start at once, so a slow one doesn't hold up the others, and
Wren waits for all of them (up to a time limit) before the first prompt. The
set of tools is fixed from then on, for the whole session (D143): the tool
list is part of every request, and changing it mid-conversation would break
prompt caching (ARCHITECTURE invariant 16).

A server writes its own diagnostics to stderr. In the full-screen app that
would scribble over the screen, so each server's stderr goes to its own log
file instead: `~/.wren/logs/mcp-NAME.log`, replaced each time Wren starts.
"""

from __future__ import annotations

import concurrent.futures
import logging
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import anyio
from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, Implementation, Tool

from wren import __version__
from wren.mcp_config import McpConfig, ServerConfig

# How long a server may take to start and list its tools. `npx` fetching a
# package on first use is the usual slow case.
START_TIMEOUT = 60  # seconds, for all servers together
# How long a single request (the handshake, a page of the tool list) may take.
REQUEST_TIMEOUT = 30
# How long one tool call may take. Escape stops a call sooner.
CALL_TIMEOUT = 300
POLL_SECONDS = 0.2  # how often a waiting call checks whether to stop

# Tools listed beyond this many pages are ignored: a server that keeps
# returning a cursor is broken, and we'd rather start than loop for ever.
MAX_PAGES = 20


class McpCallError(Exception):
    """A tool call that didn't produce a result: the message is for the model."""


@dataclass
class ServerStatus:
    """What happened when a server was started, for the notice and /mcp."""

    name: str
    command: str  # as configured, for /mcp
    log: Path
    tools: list[Tool] = field(default_factory=list)
    error: str = ""  # empty when it started
    protocol_version: str = ""  # agreed with the server during the handshake
    server_info: Implementation | None = (
        None  # the server's own name and version, if it gave one
    )

    @property
    def running(self) -> bool:
        return not self.error


class McpServers:
    """The servers Wren started this session. Use as a context manager, or
    call close(): either way every server process is stopped."""

    def __init__(
        self, config: McpConfig | None = None, log_dir: Path | None = None
    ) -> None:
        self.config = (
            config if config is not None else McpConfig()
        )  # what was read, for /mcp
        self.log_dir = (
            log_dir if log_dir is not None else Path.home() / ".wren" / "logs"
        )
        self.statuses: list[ServerStatus] = []
        self._clients: dict[str, Client] = {}
        self._tasks: list[concurrent.futures.Future[Any]] = []
        self._portal_stack = ExitStack()  # the MCP thread's event loop
        self._logs = (
            ExitStack()
        )  # the servers' log files, closed once the servers are gone
        self._portal: BlockingPortal | None = None

    def __enter__(self) -> McpServers:  # noqa: PYI034 - typing.Self needs Python 3.11; Wren supports 3.10
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- Starting -------------------------------------------------------------

    def start(
        self, configs: list[ServerConfig], timeout: float = START_TIMEOUT
    ) -> None:
        """Start every server, wait until each is ready or has failed, and
        record what happened in `statuses`. Never raises for a server's fault."""
        if not configs:
            return
        self._quiet_sdk_logging()
        self._portal = self._portal_stack.enter_context(start_blocking_portal())
        waiting: list[
            tuple[
                ServerStatus,
                concurrent.futures.Future[None],
                concurrent.futures.Future[Any],
            ]
        ] = []
        for config in configs:
            status = ServerStatus(
                config.name,
                " ".join([config.command, *config.args]),
                self.log_dir / f"mcp-{config.name}.log",
            )
            self.statuses.append(status)
            try:
                errlog = self._open_log(status.log)
            except OSError as e:
                status.error = (
                    f"couldn't open its log file {status.log}: {e.strerror or e}"
                )
                continue
            ready: concurrent.futures.Future[None] = concurrent.futures.Future()
            task = self._portal.start_task_soon(
                self._serve, config, status, errlog, ready
            )
            self._tasks.append(task)
            waiting.append((status, ready, task))

        # Wait with wait(), not result(timeout=...): since Python 3.11,
        # concurrent.futures.TimeoutError IS the built-in TimeoutError, so a
        # server that failed with a timeout of its own would be mistaken for
        # one we gave up waiting for.
        concurrent.futures.wait([ready for _, ready, _ in waiting], timeout=timeout)
        for status, ready, task in waiting:
            if not ready.done():
                task.cancel()  # or it might finish starting later, unannounced
                status.error = f"didn't start within {timeout:g} seconds"
            elif (error := ready.exception()) is not None:
                status.error = describe(
                    error
                )  # reported, never raised: one bad server mustn't stop Wren

    async def _serve(
        self,
        config: ServerConfig,
        status: ServerStatus,
        errlog: TextIO,
        ready: concurrent.futures.Future[None],
    ) -> None:
        """One server's whole life, as a single task (see the module docstring)."""
        params = StdioServerParameters(
            command=config.command,
            args=config.args,
            env=config.env or None,
            cwd=config.cwd,
        )
        client = Client(
            stdio_client(params, errlog=errlog),
            read_timeout_seconds=REQUEST_TIMEOUT,
            client_info=Implementation(name="wren", version=__version__),
        )
        try:
            async with client:
                status.protocol_version = client.protocol_version
                status.server_info = client.server_info
                status.tools = await _list_tools(client)
                self._clients[config.name] = client
                ready.set_result(None)
                await anyio.sleep_forever()  # until close() cancels this task
        except Exception as e:  # noqa: BLE001 - the server failed: tell start(), or /mcp if it was running
            if ready.done():
                status.error = f"stopped unexpectedly: {describe(e)}"
            else:
                ready.set_exception(e)
        finally:
            self._clients.pop(config.name, None)
            if (
                not ready.done()
            ):  # cancelled before it was ready (Wren exiting during start-up)
                ready.set_exception(McpCallError("stopped while starting"))

    # --- Calling ----------------------------------------------------------------

    def call(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        should_stop: Callable[[], bool] = lambda: False,
        timeout: float = CALL_TIMEOUT,
    ) -> CallToolResult:
        """Call one tool and wait for its result. Raises McpCallError, worded
        for the model, if there's no result: the server is gone, the call
        timed out, or the user stopped it."""
        client = self._clients.get(server)
        if client is None or self._portal is None:
            raise McpCallError(
                f"The MCP server {server!r} isn't running, so its tools can't be used this session."
            )
        future = self._portal.start_task_soon(_call, client, tool, arguments, timeout)
        # Wait in short slices, so Escape can stop a slow call. Cancelling the
        # future cancels the task on the MCP thread, and the SDK tells the
        # server with a `notifications/cancelled` message.
        # (wait(), not result(timeout=...), for the reason given in start().)
        while not concurrent.futures.wait([future], timeout=POLL_SECONDS).done:
            if should_stop():
                future.cancel()
                raise McpCallError("Stopped by the user before the server answered.")
        try:
            return future.result()
        except TimeoutError:  # anyio.fail_after, in _call
            raise McpCallError(
                f"The MCP server {server!r} didn't answer within {timeout:g} seconds."
            ) from None
        except Exception as e:  # noqa: BLE001 - the SDK raises several kinds; the model needs words, not a type
            raise McpCallError(
                f"The MCP server {server!r} couldn't run {tool!r}: {describe(e)}"
            ) from None

    # --- Stopping ---------------------------------------------------------------

    def close(self) -> None:
        """Stop every server, then the MCP thread. Safe to call twice."""
        for task in self._tasks:
            task.cancel()  # each _serve task leaves its `async with`, closing its server
        self._tasks.clear()
        self._portal_stack.close()  # waits for those tasks to finish, then stops the loop
        self._portal = None
        self._logs.close()

    # --- Helpers ----------------------------------------------------------------

    def _open_log(self, path: Path) -> TextIO:
        path.parent.mkdir(parents=True, exist_ok=True)
        return self._logs.enter_context(path.open("w", encoding="utf-8"))

    def _quiet_sdk_logging(self) -> None:
        """The SDK reports some problems (a server printing junk to stdout,
        say) through Python's logging, which with no handler set up prints
        to stderr: over the full-screen app. Send them to a log file instead."""
        logger = logging.getLogger("mcp")
        if logger.handlers:  # already done, by an earlier McpServers
            return
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            handler: logging.Handler = logging.FileHandler(
                self.log_dir / "mcp-sdk.log", encoding="utf-8"
            )
        except OSError:
            handler = logging.NullHandler()
        logger.addHandler(handler)
        logger.propagate = False


async def _list_tools(client: Client) -> list[Tool]:
    tools: list[Tool] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        page = await client.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.next_cursor
        if cursor is None:
            break
    return tools


async def _call(
    client: Client, tool: str, arguments: dict[str, Any], timeout: float
) -> CallToolResult:
    with anyio.fail_after(timeout):
        return await client.call_tool(tool, arguments)


def describe(error: BaseException) -> str:
    """An exception as one readable line.

    The SDK's errors often arrive wrapped in an ExceptionGroup (a task group
    collects every failure of the tasks inside it), whose own message is
    just "unhandled errors in a TaskGroup". The interesting part is inside.
    """
    # Checked by shape rather than by class: ExceptionGroup is built in only
    # from Python 3.11, and Wren still runs on 3.10 (where anyio uses a backport).
    inner: Any = getattr(error, "exceptions", None)
    while (
        isinstance(inner, tuple)
        and len(inner) == 1
        and isinstance(inner[0], BaseException)
    ):
        error = inner[0]
        inner = getattr(error, "exceptions", None)
    if isinstance(error, FileNotFoundError):
        return (
            f"couldn't find the program {error.filename!r}"
            if error.filename
            else "couldn't find the program"
        )
    text = str(error).strip() or type(error).__name__
    return text.splitlines()[0]
