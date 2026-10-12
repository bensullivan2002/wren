"""JSON-RPC 2.0 over a pair of text streams: the wire underneath ACP.

JSON-RPC is about the smallest protocol that lets two programs call each
other. Every message is one JSON object, and there are three kinds:

    a request        {"jsonrpc": "2.0", "id": 7, "method": "session/new", "params": {...}}
    its response     {"jsonrpc": "2.0", "id": 7, "result": {...}}
                  or {"jsonrpc": "2.0", "id": 7, "error": {"code": -32602, "message": "..."}}
    a notification   {"jsonrpc": "2.0", "method": "session/cancel", "params": {...}}

A request has an `id` and must be answered with the same id. A notification
has no id and gets no answer. That's nearly the whole specification.

Two things make it more interesting than it looks:

  * It runs BOTH ways. The client calls us ("session/prompt"), and in the
    middle of that we call the client ("session/request_permission") and
    wait for its answer. So a reply can't be found by "read the next line":
    the next line might be something else entirely. Each request we send is
    filed under its id, and whoever reads the matching response wakes us up.

  * The framing is one message per line. Our stdout IS the protocol, so a
    single stray print() anywhere in the program would corrupt it.
"""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from typing import Any, Iterator, TextIO

# Error codes fixed by the JSON-RPC specification.
PARSE_ERROR = -32700  # the line wasn't valid JSON
INVALID_REQUEST = -32600  # valid JSON, but not a JSON-RPC message
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class RpcError(Exception):
    """An error to send to, or received from, the other side."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ConnectionClosed(Exception):
    """The other side went away while we were waiting for its answer."""


class RequestAbandoned(Exception):
    """We stopped waiting for an answer, because the caller no longer needs it."""


@dataclass
class Incoming:
    """A request or notification from the other side."""

    method: str
    params: Any
    id: Any = None  # None means it's a notification: don't answer

    @property
    def is_request(self) -> bool:
        return self.id is not None


_CLOSED = object()  # put in a waiting request's queue when the connection ends


class Connection:
    def __init__(self, reader: TextIO, writer: TextIO) -> None:
        self._reader = reader
        self._writer = writer
        # Two threads write: the one reading requests (answering quick ones)
        # and the one running a turn (sending updates). The lock makes each
        # message go out whole, never interleaved with another.
        self._write_lock = threading.Lock()
        # Requests we've sent and not yet had answered: id -> where to put the answer.
        self._waiting: dict[int, queue.Queue] = {}
        self._waiting_lock = threading.Lock()
        self._next_id = 0

    # --- Sending ------------------------------------------------------------

    def notify(self, method: str, params: Any) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def respond(self, id: Any, result: Any) -> None:
        self._write({"jsonrpc": "2.0", "id": id, "result": result})

    def respond_error(self, id: Any, code: int, message: str) -> None:
        self._write({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})

    def request(self, method: str, params: Any, give_up: threading.Event | None = None) -> Any:
        """Call a method on the other side and wait for its result.

        Must not be called from the thread that runs `incoming()`: that
        thread is the one that would read the answer.

        `give_up`, if given, lets the wait be called off: when that event is
        set we stop waiting and raise RequestAbandoned. We can't count on the
        other side to answer a question that events have overtaken.
        """
        answer: queue.Queue = queue.Queue(maxsize=1)
        with self._waiting_lock:
            self._next_id += 1
            id = self._next_id
            self._waiting[id] = answer
        self._write({"jsonrpc": "2.0", "id": id, "method": method, "params": params})

        while True:
            try:
                message = answer.get(timeout=0.1)  # sleeps until incoming() files the response
                break
            except queue.Empty:
                if give_up is not None and give_up.is_set():
                    with self._waiting_lock:
                        self._waiting.pop(id, None)  # a late answer will now be ignored
                    raise RequestAbandoned(f"stopped waiting for {method}") from None
        if message is _CLOSED:
            raise ConnectionClosed(f"the connection closed before {method} was answered")
        if "error" in message:
            error = message["error"] or {}
            raise RpcError(error.get("code", INTERNAL_ERROR), str(error.get("message", "unknown error")))
        return message.get("result")

    def _write(self, message: dict[str, Any]) -> None:
        # json.dumps never emits a raw line break (it writes "\n" inside
        # strings as the two characters \ and n), so one message is always
        # exactly one line.
        line = json.dumps(message, ensure_ascii=False)
        with self._write_lock:
            try:
                self._writer.write(line + "\n")
                self._writer.flush()
            except (BrokenPipeError, ValueError):  # the other side has gone
                pass

    # --- Receiving ----------------------------------------------------------

    def incoming(self) -> Iterator[Incoming]:
        """Read messages until the stream ends, yielding requests and
        notifications. Responses to our own requests are delivered to
        whoever is waiting in `request()`, and not yielded."""
        try:
            for line in self._reader:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self.respond_error(None, PARSE_ERROR, "that line was not valid JSON")
                    continue
                if not isinstance(message, dict):
                    self.respond_error(None, INVALID_REQUEST, "a message must be a JSON object")
                elif isinstance(message.get("method"), str):
                    yield Incoming(message["method"], message.get("params"), message.get("id"))
                elif "id" in message:
                    self._deliver(message)
                else:
                    self.respond_error(None, INVALID_REQUEST, "a message needs a method or an id")
        finally:
            # Nobody will ever answer now: wake everyone who's still waiting.
            with self._waiting_lock:
                waiting, self._waiting = self._waiting, {}
            for answer in waiting.values():
                answer.put(_CLOSED)

    def _deliver(self, response: dict[str, Any]) -> None:
        with self._waiting_lock:
            answer = self._waiting.pop(response["id"], None) if isinstance(response["id"], int) else None
        if answer is not None:
            answer.put(response)
        # A response to something we never asked is ignored.
