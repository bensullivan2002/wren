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

# Notes the agent writes into the history, in the assistant's place, when a
# turn doesn't end with a normal reply. They keep the history valid (it must
# end with the assistant), and they tell the model, next turn, what happened.
INTERRUPTED_NOTE = (
    "[The user interrupted this turn here. Everything above really happened; "
    "anything not shown above was not done.]"
)
ERROR_NOTE = (
    "[This turn stopped here because of an error outside the conversation: {error}. "
    "Everything above really happened; anything not shown above was not done.]"
)
# Results for tool requests that were never answered because the turn stopped.
NOT_RUN = "Not run: the turn was stopped before this tool started."
CUT_SHORT = "The turn was stopped while this tool was running. It may have partly completed: check before relying on it."

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

        # Where the current turn has got to, kept on the agent (not in local
        # variables) so that if the turn is cut short we still know what had
        # already happened. See _close_unfinished_turn.
        self._partial_text = ""  # text streamed so far in the current model call
        self._results: list[ToolResultBlock] = []  # tools finished in the current batch
        self._running: str | None = None  # id of the tool call in progress, if any

    def run_turn(self, user_text: str) -> Iterator[AgentEvent]:
        """Handle one user turn, yielding events for the UI to show.

        If the turn is cut short (Ctrl-C, a network error), what had already
        happened stays in the conversation and the loose ends are tied off,
        so the conversation is always valid between turns.
        """
        self.messages.append(Message.user(user_text))
        try:
            # `yield from` passes every event from _loop up to our caller,
            # then hands us _loop's return value.
            finished = yield from self._loop()
        except BaseException as stopped_by:
            # BaseException (not Exception) so this also covers Ctrl-C, and
            # the UI abandoning the turn early.
            self._close_unfinished_turn(stopped_by)
            raise
        yield finished

    def _close_unfinished_turn(self, stopped_by: BaseException) -> None:
        """Make the history valid again after a turn was cut short, *without*
        pretending the turn didn't happen.

        Until step 3 we simply deleted the turn. That was honest while tools
        could only read. Now a turn may have edited files or run commands,
        and deleting it would leave the model with no memory of changes that
        are really on disk. So we keep everything, and finish the paperwork:

          * every tool request still waiting gets a result: the real one if
            the tool finished, otherwise a note saying it didn't run;
          * a closing note stands in for the assistant's reply, carrying any
            text that had been streamed before the stop.
        """
        last = self.messages[-1]
        if last.role == "assistant" and not last.tool_uses:
            return  # the reply was already complete; nothing is loose

        if last.role == "assistant":
            finished = {result.tool_use_id: result for result in self._results}
            answers = []
            for call in last.tool_uses:
                unfinished = CUT_SHORT if call.id == self._running else NOT_RUN
                answers.append(finished.get(call.id) or ToolResultBlock(call.id, unfinished, is_error=True))
            self.messages.append(Message("user", answers))

        if isinstance(stopped_by, Exception):
            reason = str(stopped_by).splitlines()[0] if str(stopped_by) else type(stopped_by).__name__
            note = ERROR_NOTE.format(error=reason[:200])
        else:  # KeyboardInterrupt, or the UI closing the turn
            note = INTERRUPTED_NOTE
        said = self._partial_text.rstrip()
        self.messages.append(Message.assistant(f"{said}\n\n{note}" if said else note))
        self._partial_text = ""

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
            self._partial_text = ""  # it's all in `reply` now

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
        self._partial_text = ""
        events = self.provider.stream(
            system=self.system_prompt,
            messages=self.messages,
            tools=self.tools.specs(),
            max_tokens=self.max_tokens,
        )
        with closing(events):  # close the stream even if we stop early
            for event in events:
                if isinstance(event, TextDelta):
                    self._partial_text += event.text
                elif isinstance(event, Completed):
                    completed = event
                yield event
        if completed is None:
            raise RuntimeError("provider stream ended without a Completed event")
        return completed

    def _run_tools(self, calls: list[ToolUseBlock]) -> Generator[AgentEvent, None, list[ToolResultBlock]]:
        # The model may ask for several tools in one reply. We run them one at
        # a time, in order; all the results go back together in one message.
        #
        # The results collect in self._results, not a local list: if the turn
        # is interrupted half-way through, the tools that did finish must
        # still be reported. The model needs to know its edit went through.
        self._results = []
        for call in calls:
            yield ToolStarted(call)
            self._running = call.id
            result = self.tools.run(call.name, call.input)
            self._results.append(ToolResultBlock(call.id, result.content, result.is_error))
            self._running = None
            yield ToolFinished(call, result)
        return self._results

    def reset(self) -> None:
        """Forget the conversation (the system prompt, tools and totals stay)."""
        self.messages.clear()
