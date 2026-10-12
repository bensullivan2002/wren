"""The agent: owns the conversation and runs the agent loop.

Two facts shape this class.

1. The model API is *stateless*: it remembers nothing between calls. The
   harness sends the entire conversation every time, so the agent's memory is
   just `self.messages`, a list.

2. The model can't *do* anything. It can only ask. When it wants a tool, its
   reply ends with stop reason "tool_use" and contains tool_use blocks. The
   agent runs those tools, appends the results to the conversation, and calls
   the model again. Round and round, until a reply asks for nothing. That
   cycle is the agent loop:

       call the model ──▶ did it ask for tools? ── no ──▶ the turn is done
             ▲                     │ yes
             └── send the results ◀┘ run them

The agent knows nothing about terminals. It yields events, and whoever is
driving it (today the REPL) decides how to show them.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from typing import Generator, Iterator, Union

from wren.config import DEFAULT_MAX_STEPS
from wren.messages import Completed, Message, TextDelta, ToolResultBlock, ToolUseBlock, Usage
from wren.providers import Provider
from wren.tools import ToolRegistry, ToolResult

STEP_LIMIT_NOTE = (
    "[Wren stopped this turn after {steps} model calls, its limit for one turn. "
    "The work so far is above; the user can say 'continue' to carry on.]"
)


# --- Events the agent yields, on top of the provider's TextDelta and Completed


@dataclass
class ToolStarted:
    call: ToolUseBlock


@dataclass
class ToolFinished:
    call: ToolUseBlock
    result: ToolResult


@dataclass
class TurnFinished:
    usage: Usage  # tokens for the whole turn, across every model call
    steps: int  # how many times the model was called
    hit_step_limit: bool = False


AgentEvent = Union[TextDelta, Completed, ToolStarted, ToolFinished, TurnFinished]


class Agent:
    def __init__(
        self,
        provider: Provider,
        system_prompt: str,
        max_tokens: int,
        tools: ToolRegistry | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> None:
        self.provider = provider
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.tools = tools if tools is not None else ToolRegistry()
        self.max_steps = max_steps
        self.messages: list[Message] = []
        self.usage = Usage()  # running total for this session

    def run_turn(self, user_text: str) -> Iterator[AgentEvent]:
        """Handle one user turn, yielding events for the UI to show.

        If anything goes wrong part-way (Ctrl-C, a network error), the whole
        turn is rolled back, so the conversation is always valid between turns.
        """
        checkpoint = len(self.messages)
        self.messages.append(Message.user(user_text))
        try:
            # `yield from` passes every event from _loop up to our caller,
            # then hands us _loop's return value.
            finished = yield from self._loop()
        except BaseException:
            # BaseException (not Exception) so this also covers Ctrl-C, and
            # the UI abandoning the turn early. Rolling back matters even more
            # now: a tool request without its result is invalid, and the API
            # would reject every later request in this conversation.
            del self.messages[checkpoint:]
            raise
        yield finished

    def _loop(self) -> Generator[AgentEvent, None, TurnFinished]:
        turn_usage = Usage()
        for step in range(1, self.max_steps + 1):
            completed = yield from self._call_model()
            turn_usage += completed.usage
            self.usage += completed.usage

            reply = completed.message
            if not reply.content:
                # Rare, but it happens: the API rejects an empty assistant
                # message anywhere in the history, so keep a placeholder.
                reply = Message.assistant("(no reply)")
            self.messages.append(reply)

            # We look for tool requests rather than trusting stop_reason == "tool_use":
            # a reply cut off by max_tokens can still contain one, and every
            # tool request in the history must get a result.
            if not reply.tool_uses:
                return TurnFinished(turn_usage, step)

            results = yield from self._run_tools(reply.tool_uses)
            self.messages.append(Message("user", results))

        # Out of steps. The history ends with tool results, so add a short
        # note in the assistant's place. It keeps the history valid, and it
        # tells the model next turn why it stopped.
        self.messages.append(Message.assistant(STEP_LIMIT_NOTE.format(steps=self.max_steps)))
        return TurnFinished(turn_usage, self.max_steps, hit_step_limit=True)

    def _call_model(self) -> Generator[AgentEvent, None, Completed]:
        completed: Completed | None = None
        events = self.provider.stream(
            system=self.system_prompt,
            messages=self.messages,
            tools=self.tools.specs(),
            max_tokens=self.max_tokens,
        )
        with closing(events):  # close the stream even if we stop early
            for event in events:
                if isinstance(event, Completed):
                    completed = event
                yield event
        if completed is None:
            raise RuntimeError("provider stream ended without a Completed event")
        return completed

    def _run_tools(self, calls: list[ToolUseBlock]) -> Generator[AgentEvent, None, list[ToolResultBlock]]:
        # The model may ask for several tools in one reply. We run them one at
        # a time, in order; all the results go back together in one message.
        results = []
        for call in calls:
            yield ToolStarted(call)
            result = self.tools.run(call.name, call.input)
            yield ToolFinished(call, result)
            results.append(ToolResultBlock(call.id, result.content, result.is_error))
        return results

    def reset(self) -> None:
        """Forget the conversation (the system prompt, tools and totals stay)."""
        self.messages.clear()
