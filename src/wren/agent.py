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

A third fact arrives with long conversations.

3. Because everything is re-sent every time, the conversation's size is a
   running cost: a file read once is paid for again on every later call.
   And it can't grow for ever: each model has a context window, the most
   it can take in one request. So the agent keeps track of how big the
   conversation has become, and when it nears the window (the budget: see
   `context_budget`), asks the model to summarise it and carries on from
   the summary. That's compaction: see `compact`.

The agent knows nothing about terminals. It yields events, and whoever is
driving it (a front end) decides how to show them.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Generator, Iterator
from contextlib import closing
from dataclasses import dataclass, field

from wren.config import COMPACTION_MARGIN, DEFAULT_MAX_STEPS
from wren.messages import (
    Completed,
    Message,
    TextBlock,
    TextDelta,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    history_problems,
)
from wren.prompts import COMPACT_PROMPT, COMPACTED_NOTE
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
# The same, for a conversation read back from disk (see Agent.restore).
RESUMED_NOTE = (
    "[This conversation was saved while a turn was still in progress, and has now been "
    "reopened. Everything above really happened; anything not shown above was not done.]"
)
NEVER_RECORDED = (
    "No result was recorded for this tool: Wren closed while it was pending. "
    "It may or may not have run: check before relying on it."
)

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


@dataclass
class CompactionStarted:
    automatic: bool  # True if the budget triggered it, False for /compact


@dataclass
class Compacted:
    """Compaction is over. If `error` is set it failed, and nothing changed."""

    before_tokens: int
    after_tokens: int  # an estimate, until the next model call measures it
    error: str = ""
    # What the summarising call used. It isn't a reply, so no Completed event
    # is yielded for it, and this is the only way a front end hears its cost.
    # Set even when compaction failed: the call was still paid for.
    usage: Usage = field(default_factory=Usage)


AgentEvent = (
    TextDelta
    | Completed
    | ToolStarted
    | ToolFinished
    | TurnFinished
    | CompactionStarted
    | Compacted
)


class Agent:
    def __init__(
        self,
        provider: Provider,
        system_prompt: str | Callable[[], str],
        max_tokens: int,
        tools: ToolRegistry | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        context_cap: int | None = None,
    ) -> None:
        self.provider = provider
        # The system prompt can be given as a function that builds it. Then
        # it's built now, and again each time the conversation is reset: the
        # point at which a changed memory file can be picked up for free,
        # since a new conversation starts a new cache anyway.
        self._build_system_prompt = system_prompt if callable(system_prompt) else None
        self.system_prompt: str = (
            system_prompt() if callable(system_prompt) else system_prompt
        )
        self.max_tokens = max_tokens
        self.tools = tools if tools is not None else ToolRegistry()
        self.max_steps = max_steps  # the most model calls in one turn; 0 = no limit
        # The user's cap on the budget (--context-budget). None: no cap; 0:
        # never compact automatically. See `context_budget`.
        self.context_cap = context_cap
        # The current model's context window once the provider has been asked:
        # ((provider name, model), window or None if it doesn't know). Kept
        # with the model it's for, so a switch of model (however it happens)
        # can't leave the old model's window in place. One tuple, assigned in
        # one go, because a front end's worker thread may set it.
        self._window: tuple[tuple[str, str], int | None] | None = None
        self.messages: list[Message] = []
        self.usage = Usage()  # running total for this conversation
        # How big the conversation is now, in tokens: what the next request
        # will send. We don't count it ourselves. Every reply reports how
        # many tokens its request contained, which is an exact measurement
        # by the only tokenizer that matters, and it's free.
        self.context_tokens = 0
        self.answered_by = ""  # the model the provider says wrote the latest reply
        self._models_used: dict[
            str, str
        ] = {}  # provider name -> the model last used with it
        self.last_call: Completed | None = (
            None  # the latest model call, for /usage to report on
        )

        # Where the current turn has got to, kept on the agent (not in local
        # variables) so that if the turn is cut short we still know what had
        # already happened. See _close_unfinished_turn.
        # Whoever is recording the conversation (see sessions.Journal), if
        # anyone. Every change to the history is reported to it as it happens.
        self.journal = None

        self._partial_text = ""  # text streamed so far in the current model call
        self._results: list[ToolResultBlock] = []  # tools finished in the current batch
        self._running: str | None = None  # id of the tool call in progress, if any

    def run_turn(self, user_text: str) -> Iterator[AgentEvent]:
        """Handle one user turn, yielding events for the UI to show.

        If the turn is cut short (Ctrl-C, a network error), what had already
        happened stays in the conversation and the loose ends are tied off,
        so the conversation is always valid between turns.
        """
        # Between turns is the one moment the history is whole and nothing is
        # in flight, so that's when we tidy it. (Within a long turn it can
        # still grow past the budget: that's what the budget's margin below
        # the window is for.)
        self.look_up_context_window()
        if self.context_budget and self.context_tokens >= self.context_budget:
            yield from self.compact(automatic=True)

        self._add(Message.user(user_text))
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

    def _add(self, message: Message) -> None:
        """The one way a message joins the history. Because every addition
        comes through here, the journal can't miss one."""
        self.messages.append(message)
        if self.journal is not None:
            self.journal.append(message)

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
        if isinstance(stopped_by, Exception):
            reason = (
                str(stopped_by).splitlines()[0]
                if str(stopped_by)
                else type(stopped_by).__name__
            )
            note = ERROR_NOTE.format(error=reason[:200])
        else:  # KeyboardInterrupt, or the UI closing the turn
            note = INTERRUPTED_NOTE
        finished = {result.tool_use_id: result for result in self._results}
        self._tie_off(note, finished, running=self._running, said=self._partial_text)
        self._partial_text = ""

    def _tie_off(
        self,
        note: str,
        finished: dict[str, ToolResultBlock],
        running: str | None = None,
        said: str = "",
        not_run: str = NOT_RUN,
    ) -> None:
        """Answer any tool requests still waiting, and close with `note`."""
        last = self.messages[-1]
        if last.role == "assistant" and not last.tool_uses:
            return  # the reply was already complete; nothing is loose

        if last.role == "assistant":
            answers = []
            for call in last.tool_uses:
                unfinished = CUT_SHORT if call.id == running else not_run
                answers.append(
                    finished.get(call.id)
                    or ToolResultBlock(call.id, unfinished, is_error=True)
                )
            self._add(Message("user", answers))

        said = said.rstrip()
        self._add(Message.assistant(f"{said}\n\n{note}" if said else note))

    def _loop(self) -> Generator[AgentEvent, None, TurnFinished]:
        turn_usage = Usage()
        # With no limit, count 1, 2, 3... for ever: the turn ends when a reply
        # asks for no tools, or when you stop it (Escape, Ctrl-C).
        steps = range(1, self.max_steps + 1) if self.max_steps else itertools.count(1)
        for step in steps:
            completed = yield from self._call_model()
            turn_usage += completed.usage
            self.usage += completed.usage

            # This request's input, plus the reply it produced, is what the
            # next request will carry.
            self.context_tokens = (
                completed.usage.sent_tokens + completed.usage.output_tokens
            )
            self.answered_by = completed.model
            self.last_call = completed
            if self.journal is not None:
                self.journal.spent(completed.usage, self.context_tokens)

            reply = completed.message
            if not reply.text and not reply.tool_uses:
                # Rare, but it happens: a reply with nothing to show. The API
                # rejects an empty assistant message anywhere in the history,
                # so add a placeholder (after any reasoning, which stays).
                reply.content.append(TextBlock("(no reply)"))
            self._add(reply)
            self._partial_text = ""  # it's all in `reply` now

            # We look for tool requests rather than trusting stop_reason == "tool_use":
            # a reply cut off by max_tokens can still contain one, and every
            # tool request in the history must get a result.
            if not reply.tool_uses:
                return TurnFinished(turn_usage, step)

            results = yield from self._run_tools(reply.tool_uses)
            self._add(Message("user", results))

        # Out of steps. The history ends with tool results, so add a short
        # note in the assistant's place. It keeps the history valid, and it
        # tells the model next turn why it stopped.
        self._add(Message.assistant(STEP_LIMIT_NOTE.format(steps=self.max_steps)))
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

    def _run_tools(
        self, calls: list[ToolUseBlock]
    ) -> Generator[AgentEvent, None, list[ToolResultBlock]]:
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
            self._results.append(
                ToolResultBlock(call.id, result.content, result.is_error)
            )
            self._running = None
            yield ToolFinished(call, result)
        return self._results

    # --- Compaction -----------------------------------------------------------

    def look_up_context_window(self) -> None:
        """Ask the provider for the current model's context window, unless
        we already have. It may be a network call, so a front end with a
        screen to keep moving calls this from a worker thread; `run_turn`
        calls it too. Never raises: if the provider can't be reached, the
        window stays unknown and the next turn asks again."""
        model = (self.provider.name, self.provider.model)
        if self._window is not None and self._window[0] == model:
            return
        try:
            window = self.provider.context_window()
        except Exception:  # noqa: BLE001 - any failure leaves the window unknown
            return  # unknown for now; no automatic compaction until it's known
        if (
            self.provider.name,
            self.provider.model,
        ) == model:  # not switched while we asked
            self._window = (model, window)

    @property
    def context_window_checked(self) -> bool:
        """Whether the provider has been asked about the current model's
        window (whether or not it knew)."""
        return self._window is not None and self._window[0] == (
            self.provider.name,
            self.provider.model,
        )

    @property
    def context_window(self) -> int | None:
        """The current model's context window, or None if it isn't known
        (yet, or at all)."""
        found = self._window  # read once: a worker thread may replace it meanwhile
        if found is None or found[0] != (self.provider.name, self.provider.model):
            return None
        return found[1]

    @property
    def context_budget(self) -> int:
        """How big the conversation may grow before it's compacted, in
        tokens. 0 means it isn't compacted automatically.

        Just short of the model's window: the window, less room for a reply,
        less a margin (config.COMPACTION_MARGIN explains both). With a
        200,000-token window and 32,000-token replies, that's 148,000.
        The user's cap, if set, can only bring it lower. If the window isn't
        known, only the cap applies; with neither, it's 0.
        """
        if self.context_cap == 0:
            return 0
        budgets = []
        window = self.context_window
        if window:
            budgets.append(window - self.max_tokens - int(window * COMPACTION_MARGIN))
        if self.context_cap:
            budgets.append(self.context_cap)
        return min(budgets, default=0)

    def compact(self, automatic: bool = False) -> Iterator[AgentEvent]:
        """Replace the conversation with the model's own summary of it.

        One extra model call: the whole conversation, plus a request to
        summarise it. Then the history becomes two messages: a note saying
        what happened, and the summary. Everything after that costs a
        fraction of what it did.

        It is lossy, and there's no way round that. The summary keeps what
        the prompt asks for (see COMPACT_PROMPT) and the rest is gone: the
        model can no longer see files it read or exactly what was said.

        If anything goes wrong, the history is left exactly as it was. The
        swap is the last step, and happens only once we hold a good summary.
        """
        if len(self.messages) < 2:
            yield Compacted(
                self.context_tokens,
                self.context_tokens,
                error="there's nothing to compact yet",
            )
            return
        before = self.context_tokens
        yield CompactionStarted(automatic)
        completed: Completed | None = None
        try:
            # The same system prompt and tools as every other call, with the
            # request added on the end. Since the beginning matches what was
            # sent last time, this call reads the conversation from the cache.
            events = self.provider.stream(
                system=self.system_prompt,
                messages=[*self.messages, Message.user(COMPACT_PROMPT)],
                tools=self.tools.specs(),
                max_tokens=self.max_tokens,
            )
            with closing(events):
                for (
                    event
                ) in events:  # the summary isn't shown as it streams: it's not a reply
                    if isinstance(event, Completed):
                        completed = event
            if completed is None:
                raise RuntimeError("the provider's stream ended early")
            self.usage += completed.usage
            summary = completed.message.text.strip()
            if completed.message.tool_uses or not summary:
                raise RuntimeError("the model didn't write a summary")
            if completed.stop_reason == "max_tokens":
                raise RuntimeError("the summary was cut off by the max_tokens limit")
        except Exception as e:  # noqa: BLE001 - compaction fails safe (D47)
            if completed is not None and self.journal is not None:
                self.journal.spent(
                    completed.usage, before
                )  # it cost something even though it failed
            spent = completed.usage if completed is not None else Usage()
            yield Compacted(
                before, before, error=str(e) or type(e).__name__, usage=spent
            )
            return

        # Only the summary's text is kept. Any reasoning that came with it
        # belonged to the conversation we've just let go of.
        self.messages[:] = [Message.user(COMPACTED_NOTE), Message.assistant(summary)]
        if self.journal is not None:
            self.journal.replace(self.messages)
        # The summary's length is a fair first estimate of the new size. The
        # next model call will replace it with a real measurement.
        self.context_tokens = completed.usage.output_tokens
        if self.journal is not None:
            self.journal.spent(completed.usage, self.context_tokens)
        yield Compacted(before, self.context_tokens, usage=completed.usage)

    # --- Sessions and models -----------------------------------------------------

    def restore(
        self,
        messages: list[Message],
        usage: Usage | None = None,
        context_tokens: int | None = None,
    ) -> None:
        """Take up a conversation read back from disk.

        A saved conversation can stop anywhere: Wren may have been closed,
        or killed, in the middle of a turn. So the first thing we do is what
        we'd do after an interruption (step 3): answer any tool request left
        hanging, and add a note saying where things stopped. If the history
        still doesn't satisfy the rules after that, we refuse it. Better no
        conversation than one the API will reject on every request.
        """
        previous = list(self.messages)
        self.messages[:] = messages
        if self.messages:
            self._tie_off(RESUMED_NOTE, finished={}, not_run=NEVER_RECORDED)
        problems = history_problems(self.messages)
        if problems:
            self.messages[:] = previous
            raise ValueError(
                "the saved conversation is not usable: " + "; ".join(problems)
            )
        # The totals and the size as they were when it was saved, if the
        # session recorded them. If not (an older file), start the totals at
        # zero and guess the size at about four characters to a token; the
        # next model call measures it properly.
        self.usage = usage if usage is not None else Usage()
        if context_tokens is None:
            context_tokens = sum(_characters(message) for message in self.messages) // 4
        self.context_tokens = context_tokens

    def set_model(self, model: str) -> None:
        """Switch to a different model, from the next request on.

        The conversation carries over: it's just messages, and any model can
        read them. Two things don't. The cache is kept per model, so the new
        one reads everything afresh once, at full price. And the old model's
        private reasoning means nothing to the new one, which ignores it.
        """
        self.provider.model = model
        # Effort goes back to the new model's default. The levels differ
        # from model to model, and a level this one doesn't take would make
        # every request fail.
        self.provider.effort = None
        if self.journal is not None:
            self.journal.model_changed(self.provider.name, model, None)

    def set_provider(self, provider: Provider) -> None:
        """Hand the conversation to a different provider, from the next
        request on.

        This works for the reason switching model does, one level up: the
        history is Wren's own neutral messages, and every provider knows how
        to render them. The old provider's private reasoning doesn't cross
        (each provider leaves out reasoning it didn't write), and the new
        provider reads the conversation afresh, at full price, once.
        """
        self._models_used[self.provider.name] = self.provider.model
        self.provider = provider
        if self.journal is not None:
            self.journal.model_changed(provider.name, provider.model, provider.effort)

    def model_last_used_with(self, provider_name: str) -> str | None:
        """The model this conversation last used with that provider, if any,
        so switching back returns to it and not to the provider's default."""
        return self._models_used.get(provider_name)

    def set_effort(self, effort: str | None) -> None:
        """Change how hard the model works on each reply, from the next
        request on. None means its own default."""
        self.provider.effort = effort
        if self.journal is not None:
            self.journal.model_changed(self.provider.name, self.provider.model, effort)

    def reset(self) -> None:
        """Forget the conversation and its totals (the tools stay), and
        rebuild the system prompt if we were given a way to."""
        self.messages.clear()
        self.context_tokens = 0
        self.usage = Usage()
        if self.journal is not None:
            self.journal.reset()
        if self._build_system_prompt is not None:
            self.system_prompt = self._build_system_prompt()


def _characters(message: Message) -> int:
    total = 0
    for block in message.content:
        if isinstance(block, TextBlock):
            total += len(block.text)
        elif isinstance(block, ToolResultBlock):
            total += len(block.content)
        elif isinstance(block, ToolUseBlock):
            total += len(str(block.input))
    return total
