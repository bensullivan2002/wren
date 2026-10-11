"""A pretend model that needs no network and no API key.

Two uses:
  1. Tests: give it a script of replies, then check what the agent did and
     what it sent (every call is recorded in `self.calls`).
  2. Offline demo: with no script it echoes your message back, word by word,
     so you can try the terminal loop for free: `wren --provider fake`.

Because it implements the same Provider interface as the real thing, the
agent can't tell the difference. That's the provider boundary paying off.
"""

from __future__ import annotations

import re
import time
from typing import Iterator

from wren.messages import Completed, Message, StreamEvent, TextDelta, Usage


class FakeProvider:
    name = "fake"

    def __init__(self, replies: list[str] | None = None, delay: float = 0.0) -> None:
        self.replies = list(replies) if replies is not None else None
        self.model = "echo" if replies is None else "scripted"
        self.delay = delay  # seconds between words, to make streaming visible
        self.calls: list[dict] = []  # what we were sent, for test assertions

    def stream(
        self,
        *,
        system: str,
        messages: list[Message],
        max_tokens: int,
    ) -> Iterator[StreamEvent]:
        self.calls.append({"system": system, "messages": list(messages), "max_tokens": max_tokens})

        if self.replies is None:
            reply = f"You said: {messages[-1].text}"
        elif self.replies:
            reply = self.replies.pop(0)
        else:
            raise RuntimeError("FakeProvider ran out of scripted replies")

        for word in re.findall(r"\S+\s*", reply):  # each word keeps its trailing space
            yield TextDelta(word)
            if self.delay:
                time.sleep(self.delay)

        sent_chars = len(system) + sum(len(m.text) for m in messages)
        yield Completed(
            message=Message.assistant(reply),
            stop_reason="end_turn",
            # Rough rule of thumb: one token is about four characters of English.
            usage=Usage(input_tokens=sent_chars // 4, output_tokens=len(reply) // 4),
        )
