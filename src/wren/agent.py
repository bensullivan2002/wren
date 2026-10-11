"""The agent: owns the conversation and runs it against a model.

The key fact behind this class: the model API is *stateless*. It remembers
nothing between calls. Every time, the harness sends the entire conversation
so far, and the model continues it. So the agent's "memory" is nothing more
than `self.messages`, a list that grows by two messages per turn.

The agent knows nothing about terminals. It yields events, and whoever is
driving it (today the REPL, later maybe a test, a TUI or a web UI) decides
how to show them. That separation is why we can test it without a screen.
"""

from __future__ import annotations

from contextlib import closing
from typing import Iterator

from wren.messages import Completed, Message, StreamEvent, Usage
from wren.providers import Provider


class Agent:
    def __init__(self, provider: Provider, system_prompt: str, max_tokens: int) -> None:
        self.provider = provider
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.messages: list[Message] = []
        self.usage = Usage()  # running total for this session

    def run_turn(self, user_text: str) -> Iterator[StreamEvent]:
        """Handle one user turn, yielding stream events for the UI to show.

        Today a turn is a single round trip: send, stream the reply, done.
        In step 2 this becomes the real agent loop: when the model stops with
        stop_reason "tool_use", we run the tools it asked for, append their
        results, and call the model again, until it stops with "end_turn".
        """
        checkpoint = len(self.messages)
        self.messages.append(Message.user(user_text))
        try:
            completed: Completed | None = None
            events = self.provider.stream(
                system=self.system_prompt,
                messages=self.messages,
                max_tokens=self.max_tokens,
            )
            with closing(events):  # close the stream even if we stop early
                for event in events:
                    if isinstance(event, Completed):
                        completed = event
                    yield event

            if completed is None:
                raise RuntimeError("provider stream ended without a Completed event")
            self.messages.append(completed.message)
            self.usage += completed.usage

        except BaseException:
            # Interrupted (Ctrl-C) or failed part-way through. Roll the
            # conversation back to how it was before this turn. Otherwise the
            # history keeps a question with no answer, and the model sees it
            # next time and may answer both. From step 2 it matters even more:
            # a tool request left without its result is rejected by the API.
            # BaseException (not Exception) so this also covers Ctrl-C, and
            # the UI abandoning the turn early.
            del self.messages[checkpoint:]
            raise

    def reset(self) -> None:
        """Forget the conversation (the system prompt and totals stay)."""
        self.messages.clear()
