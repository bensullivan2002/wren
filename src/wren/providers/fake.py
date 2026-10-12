"""A pretend model that needs no network and no API key.

Two uses:

1. Tests: give it a script of replies, then check what the agent did and
   what it sent (every call is recorded in `self.calls`). A reply is either
   a string (plain text) or a Message, which may contain ToolUseBlocks to
   make the agent run tools.

2. Offline demo (no script): `wren --provider fake`. It understands a few
   commands, so you can watch the agent loop work without paying for a model:

       ls [pattern]                  asks for list_files
       read <path>                   asks for read_file
       search <regex>                asks for search
       write <path> <text>           asks for write_file
       edit <path> <old> => <new>    asks for edit_file
       run <command>                 asks for shell

   After the tool runs, it replies with a one-line note about the result.
   Anything else you type is echoed back.

Because it implements the same Provider interface as the real thing, the
agent can't tell the difference. That's the provider boundary paying off.
"""

from __future__ import annotations

import json
import re
import time
from typing import Iterator, Sequence

from wren.messages import (
    Completed,
    Message,
    StreamEvent,
    TextBlock,
    TextDelta,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)
from wren.prompts import COMPACT_PROMPT

class FakeProvider:
    name = "fake"

    def __init__(self, replies: list[str | Message] | None = None, delay: float = 0.0) -> None:
        self.replies = list(replies) if replies is not None else None
        self.model = "demo" if replies is None else "scripted"
        self.delay = delay  # seconds between words, to make streaming visible
        self.calls: list[dict] = []  # what we were sent, for test assertions
        self._next_id = 0

    def stream(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: Sequence[ToolSpec] = (),
        max_tokens: int,
    ) -> Iterator[StreamEvent]:
        self.calls.append(
            {"system": system, "messages": list(messages), "tools": list(tools), "max_tokens": max_tokens}
        )

        if self.replies is None:
            reply = self._demo_reply(messages)
        elif self.replies:
            reply = self.replies.pop(0)
        else:
            raise RuntimeError("FakeProvider ran out of scripted replies")
        if isinstance(reply, str):
            reply = Message.assistant(reply)

        for block in reply.content:
            if isinstance(block, TextBlock):
                for word in re.findall(r"\S+\s*", block.text):  # each word keeps its trailing space
                    yield TextDelta(word)
                    if self.delay:
                        time.sleep(self.delay)

        # Rough rule of thumb: one token is about four characters of English.
        sent = len(system) + sum(_chars(m) for m in messages) + sum(len(json.dumps(t.input_schema)) for t in tools)
        yield Completed(
            message=reply,
            stop_reason="tool_use" if reply.tool_uses else "end_turn",
            usage=Usage(input_tokens=sent // 4, output_tokens=_chars(reply) // 4),
        )

    def _demo_reply(self, messages: list[Message]) -> Message:
        last = messages[-1]
        if last.text == COMPACT_PROMPT:  # Wren is compacting: play along
            asked = [m.text for m in messages[:-1] if m.role == "user" and m.text]
            return Message.assistant(
                "## What the user wants\n(fake model) A pretend summary. "
                f"There were {len(messages) - 1} messages; the user's last one was: {asked[-1] if asked else '(none)'}"
            )
        if last.tool_results:
            result = last.tool_results[0]
            lines = result.content.splitlines() or [""]
            verdict = "returned an error" if result.is_error else f"returned {len(lines)} line(s)"
            return Message.assistant(f"(fake model) The tool {verdict}. The first line is: {lines[0]}")

        command, _, arg = last.text.strip().partition(" ")
        arg = arg.strip()
        call = None
        if command == "ls":
            call = ("list_files", {"pattern": arg} if arg else {})
        elif command == "read" and arg:
            call = ("read_file", {"path": arg})
        elif command == "search" and arg:
            call = ("search", {"pattern": arg})
        elif command == "run" and arg:
            call = ("shell", {"command": arg})
        elif command == "write" and " " in arg:
            path, _, text = arg.partition(" ")
            call = ("write_file", {"path": path, "content": text + "\n"})
        elif command == "edit" and " => " in arg and " " in arg.split(" => ")[0]:
            path, _, change = arg.partition(" ")
            old, _, new = change.partition(" => ")
            call = ("edit_file", {"path": path, "old_string": old, "new_string": new})
        if call is None:
            return Message.assistant(f"You said: {last.text}")

        self._next_id += 1
        name, args = call
        return Message(
            "assistant",
            [TextBlock(f"(fake model) I'll use {name}."), ToolUseBlock(f"fake_{self._next_id}", name, args)],
        )


def _chars(message: Message) -> int:
    total = 0
    for block in message.content:
        if isinstance(block, TextBlock):
            total += len(block.text)
        elif isinstance(block, ToolUseBlock):
            total += len(block.name) + len(json.dumps(block.input))
        elif isinstance(block, ToolResultBlock):
            total += len(block.content)
    return total
