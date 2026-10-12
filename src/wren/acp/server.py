"""Wren as an ACP agent: a second front end, for other programs to drive.

The Agent Client Protocol (ACP) does for coding agents what the Language
Server Protocol did for language tooling. An editor or terminal UI (the
"client", for us Toad) starts the agent as a child process and talks to it
in JSON-RPC over stdin and stdout. Any client can then drive any agent.

This module is a translator, and nothing more. The agent already describes
a turn as a stream of events, and already asks permission through a
callback. So everything here is one of two mappings:

    Wren                                 ACP
    ----------------------------------   ------------------------------------
    TextDelta                        ->  session/update  agent_message_chunk
    ToolStarted                      ->  session/update  tool_call
    ToolFinished                     ->  session/update  tool_call_update
    Permissions.ask(request)         ->  session/request_permission (and wait)
    closing the turn's generator     <-  session/cancel
    TurnFinished / how it ended      ->  the session/prompt response

The terminal REPL (repl.py) is the same translation aimed at a screen. Two
front ends over one agent, neither needing the agent to change, is the
payoff of keeping the agent free of I/O since step 1.

Threads: the main thread reads stdin and answers quick requests. Each
session/prompt runs in its own thread, because a turn takes a long time and
stdin must keep being read meanwhile: that's where a cancel arrives, and
where the answer to our own permission request arrives.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from wren import __version__
from wren.acp.jsonrpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    Connection,
    ConnectionClosed,
    Incoming,
    RequestAbandoned,
    RpcError,
)
from wren.agent import Agent, ToolFinished, ToolStarted, TurnFinished
from wren.messages import Completed, TextDelta, ToolUseBlock
from wren.tools import Decision, PermissionRequest, Permissions, ToolResult

PROTOCOL_VERSION = 1

# ACP sorts tool calls into kinds so a client can pick an icon or a layout.
# This table is the only place Wren's tool names meet ACP's vocabulary; a
# tool that isn't listed is shown as "other".
TOOL_KINDS = {
    "list_files": "search",
    "read_file": "read",
    "search": "search",
    "edit_file": "edit",
    "write_file": "edit",
    "shell": "execute",
}

MAX_SHOWN_CHARS = 4_000  # how much of a command's output we send for display

# Builds the agent for a new session: given the session's working directory
# and its Permissions, return an Agent wired to them. Supplied by cli.py, so
# this module needn't know about providers or config.
AgentFactory = Callable[[Path, Permissions], Agent]


@dataclass
class Session:
    """One conversation. A client may open several; each has its own agent."""

    id: str
    agent: Agent
    cancelled: threading.Event = field(default_factory=threading.Event)
    busy: threading.Lock = field(default_factory=threading.Lock)  # held while a turn runs
    current_call: ToolUseBlock | None = None  # the tool call in progress, if any


class AcpServer:
    def __init__(self, connection: Connection, make_agent: AgentFactory) -> None:
        self.connection = connection
        self.make_agent = make_agent
        self.sessions: dict[str, Session] = {}
        self._requests = {
            "initialize": self.initialize,
            "session/new": self.new_session,
            "session/prompt": self.prompt,
        }
        self._notifications = {"session/cancel": self.cancel}

    def serve(self) -> None:
        """Handle messages until the client closes the connection."""
        turns = []
        for message in self.connection.incoming():
            if message.method == "session/prompt":
                thread = threading.Thread(target=self._handle, args=(message,), daemon=True)
                thread.start()
                turns.append(thread)
            else:
                self._handle(message)
        # The client has gone. Stop any turn still running, and let it tidy
        # its history before the process exits.
        for session in self.sessions.values():
            session.cancelled.set()
        for thread in turns:
            thread.join(timeout=5)

    def _handle(self, message: Incoming) -> None:
        notified = self._notifications.get(message.method)
        if notified is not None:
            notified(message.params or {})
            # The spec says session/cancel is a notification, with no reply.
            # But be liberal in what you accept: if a client sends it as a
            # request, it's waiting for an answer, so give it one.
            if message.is_request:
                self.connection.respond(message.id, None)
            return
        if not message.is_request:
            return  # unknown notifications are ignored, as the spec asks
        handler = self._requests.get(message.method)
        if handler is None:
            self.connection.respond_error(message.id, METHOD_NOT_FOUND, f"Wren doesn't support {message.method}")
            return
        try:
            result = handler(message.params or {})
        except RpcError as e:
            self.connection.respond_error(message.id, e.code, e.message)
        except Exception as e:  # network trouble, a bad API key, or a bug
            self.connection.respond_error(message.id, INTERNAL_ERROR, f"{type(e).__name__}: {e}")
        else:
            self.connection.respond(message.id, result)

    # --- The methods a client calls -------------------------------------------

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """The handshake: agree a protocol version and say what we can do."""
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": False,  # conversations aren't saved yet (roadmap step 7)
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
            },
            "agentInfo": {"name": "wren", "title": "Wren", "version": __version__},
            "authMethods": [],  # the API key comes from the environment
        }

    def new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = params.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise RpcError(INVALID_PARAMS, "session/new needs 'cwd', an absolute path")
        if not Path(cwd).is_dir():
            raise RpcError(INVALID_PARAMS, f"cwd is not a folder: {cwd}")

        session_id = f"wren-{uuid.uuid4().hex[:12]}"
        permissions = Permissions()
        session = Session(session_id, self.make_agent(Path(cwd), permissions))
        # The two hooks the tools layer offers a front end (see cli.py for
        # the REPL's versions of the same wiring):
        permissions.ask = lambda request: self._ask_permission(session, request)
        session.agent.tools.context.should_stop = session.cancelled.is_set
        self.sessions[session_id] = session
        return {"sessionId": session_id}

    def prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run one turn, reporting progress as it happens. The response,
        sent when the turn is over, says only why it stopped."""
        session = self._session(params)
        text = _prompt_text(params.get("prompt"))
        if not text.strip():
            raise RpcError(INVALID_PARAMS, "the prompt has no text")
        if not session.busy.acquire(blocking=False):
            raise RpcError(INVALID_PARAMS, "this session is already running a turn")
        try:
            session.cancelled.clear()
            stop_reason = "end_turn"
            # `closing` is what makes cancelling work. Leaving this block
            # early closes the generator, which raises GeneratorExit inside
            # the agent, which makes it tidy the history (step 3). So
            # cancelling a turn here is the same thing as Ctrl-C in the REPL.
            with closing(session.agent.run_turn(text)) as events:
                for event in events:
                    if session.cancelled.is_set():
                        break
                    self._report(session, event)
                    if isinstance(event, Completed) and event.stop_reason in ("max_tokens", "refusal"):
                        stop_reason = event.stop_reason
                    elif isinstance(event, TurnFinished) and event.hit_step_limit:
                        stop_reason = "max_turn_requests"
            return {"stopReason": "cancelled" if session.cancelled.is_set() else stop_reason}
        finally:
            session.current_call = None
            session.busy.release()

    def cancel(self, params: dict[str, Any]) -> None:
        """Ask a running turn to stop. It's a notification, so there's no
        reply: the turn's own session/prompt response will say "cancelled".

        Stopping is cooperative: the turn notices at its next event, and a
        running shell command within a fraction of a second. The one long
        wait we can't cut short is for the model's first words.
        """
        session = self.sessions.get(params.get("sessionId", ""))
        if session is not None:
            session.cancelled.set()

    def _session(self, params: dict[str, Any]) -> Session:
        session = self.sessions.get(params.get("sessionId", ""))
        if session is None:
            raise RpcError(INVALID_PARAMS, f"no such session: {params.get('sessionId')!r}")
        return session

    # --- Wren's events, as ACP updates ------------------------------------------

    def _update(self, session: Session, update: dict[str, Any]) -> None:
        self.connection.notify("session/update", {"sessionId": session.id, "update": update})

    def _report(self, session: Session, event: Any) -> None:
        if isinstance(event, TextDelta):
            self._update(
                session, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": event.text}}
            )

        elif isinstance(event, ToolStarted):
            session.current_call = event.call
            root = session.agent.tools.context.root
            self._update(
                session,
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": event.call.id,
                    "title": tool_title(event.call),
                    "kind": TOOL_KINDS.get(event.call.name, "other"),
                    "status": "in_progress",
                    "rawInput": event.call.input,
                    "locations": _locations(event.call, root),
                },
            )

        elif isinstance(event, ToolFinished):
            session.current_call = None
            self._update(
                session,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": event.call.id,
                    "status": "failed" if event.result.is_error else "completed",
                    "content": _result_content(event.call, event.result),
                },
            )
        # Completed and TurnFinished have no update of their own: what they
        # say travels in the session/prompt response.

    # --- Permission: Wren asks, the client's user answers -------------------------

    def _ask_permission(self, session: Session, request: PermissionRequest) -> Decision:
        """Called by the tools layer, on the turn's thread, in the middle of
        a tool call. Blocks until the client's user has chosen."""
        options = [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}]
        if request.scope is not None:
            options.append(
                {
                    "optionId": "always",
                    "name": f"Always allow {request.scope_label} this session",
                    "kind": "allow_always",
                }
            )
        options.append({"optionId": "deny", "name": "Deny", "kind": "reject_once"})

        call = session.current_call
        tool_call = {
            "toolCallId": call.id if call else "unknown",
            "title": request.title,
            "content": [_change_content(request.change) if request.change else _text_content(request.detail)],
        }
        try:
            # `give_up`: the spec says a client must answer "cancelled" when
            # its user cancels the turn at this prompt, but not every client
            # does. So a cancel also ends the wait from our side.
            answer = self.connection.request(
                "session/request_permission",
                {"sessionId": session.id, "toolCall": tool_call, "options": options},
                give_up=session.cancelled,
            )
        except RequestAbandoned:
            return Decision.DENY
        except (RpcError, ConnectionClosed):
            # No clear yes means no. If the client has gone, stop the turn too.
            session.cancelled.set()
            return Decision.DENY

        outcome = (answer or {}).get("outcome") or {}
        if outcome.get("outcome") != "selected":  # "cancelled": the user stopped the turn instead
            session.cancelled.set()
            return Decision.DENY
        return {"allow": Decision.ALLOW, "always": Decision.ALWAYS}.get(outcome.get("optionId"), Decision.DENY)


# --- Small translations ----------------------------------------------------------


def tool_title(call: ToolUseBlock) -> str:
    """A short human title for a tool call: "Read src/app.py"."""
    args = call.input if isinstance(call.input, dict) else {}
    path, pattern = args.get("path"), args.get("pattern")
    if call.name == "read_file" and path:
        return f"Read {path}"
    if call.name == "list_files":
        return f"List {pattern or '*'}" + (f" in {path}" if path else "")
    if call.name == "search" and pattern:
        return f"Search for {pattern!r}"
    if call.name == "edit_file" and path:
        return f"Edit {path}"
    if call.name == "write_file" and path:
        return f"Write {path}"
    if call.name == "shell" and isinstance(args.get("command"), str):
        first_line = (args["command"].strip().splitlines() or [""])[0]
        return first_line if len(first_line) <= 80 else first_line[:79] + "…"
    return call.name


def _locations(call: ToolUseBlock, root: Path) -> list[dict[str, Any]]:
    """The file a tool call is about, so the client can offer to open it.
    ACP wants absolute paths; the model gives ones relative to the root."""
    args = call.input if isinstance(call.input, dict) else {}
    path = args.get("path")
    if call.name not in ("read_file", "edit_file", "write_file") or not isinstance(path, str):
        return []
    return [{"path": str(root / path)}]


def _result_content(call: ToolUseBlock, result: ToolResult) -> list[dict[str, Any]]:
    """What the client should show for a finished tool call.

    Not what the model saw: the model gets all of `result.content`, but a
    person watching wants the gist. A diff for an edit, the output of a
    command, the error if it failed, and a one-line summary for the rest.
    """
    if result.is_error:
        return [_text_content(result.content)]
    if result.change is not None:
        return [_change_content(result.change)]
    if call.name == "shell":
        output = result.content
        if len(output) > MAX_SHOWN_CHARS:
            output = "…\n" + output[-MAX_SHOWN_CHARS:]
        return [_text_content(output)]
    return [_text_content(result.summary or "done")]


def _text_content(text: str) -> dict[str, Any]:
    return {"type": "content", "content": {"type": "text", "text": text}}


def _change_content(change) -> dict[str, Any]:
    return {"type": "diff", "path": str(change.path), "oldText": change.before, "newText": change.after}


def _prompt_text(blocks: Any) -> str:
    """Flatten an ACP prompt (a list of content blocks) into the plain text
    Wren's agent takes. Files the user attached become labelled text."""
    if not isinstance(blocks, list):
        raise RpcError(INVALID_PARAMS, "session/prompt needs 'prompt', a list of content blocks")
    parts = []
    for block in blocks:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind == "resource_link":  # a mention of a file; the model can read it with its tools
            parts.append(f"[Attached: {block.get('uri', '')}]")
        elif kind == "resource":  # a file's contents, sent along with the prompt
            resource = block.get("resource") or {}
            if "text" in resource:
                parts.append(f'<file uri="{resource.get("uri", "")}">\n{resource["text"]}\n</file>')
        # Images and audio: we said in `initialize` that we don't take them.
    return "\n\n".join(parts)
