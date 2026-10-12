"""A running account of this run of Wren: tokens, money, time and tools.

The footer at the bottom of the app shows these numbers; this module keeps
them. It knows nothing about screens. A front end tells it when a turn
starts and ends, hands it every event the agent yields, and reads the
numbers back whenever it redraws.

Everything here counts from when Wren opened, not from the start of the
conversation. A resumed conversation brings its old token totals back
(`/usage` shows those), but not the time it took, and a rate such as
pounds per minute only means something if the money and the minutes start
at the same moment. `/clear` doesn't reset it either: the money was spent.

Times come from `time.monotonic`, a clock that only goes forwards. The wall
clock (`time.time`) can jump when the computer corrects its time or the
clocks go back, and a turn could then appear to take minus an hour.
"""

from __future__ import annotations

import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from wren.agent import Compacted, ToolFinished, ToolStarted
from wren.messages import Completed, Prices, TextDelta, ToolUseBlock, Usage
from wren.providers import Provider
from wren.tools.registry import DECLINED


class RunStats:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock  # a test can pass its own, and move time by hand
        self.opened_at = clock()

        # Spending.
        self.usage = Usage()  # every model call since Wren opened, compaction included
        self.cost = Decimal(0)  # US dollars, for the calls whose model has known prices
        self.unpriced_calls = 0  # calls to a model with unknown prices: not in `cost`
        self.compactions = 0

        # Turns.
        self.turns = 0  # messages you've sent
        self.finished_turns = 0
        self.finished_turn_tokens = 0  # across all finished turns, for the average
        self.last_turn_seconds: float | None = None
        self.last_turn_tokens: int | None = None
        self.first_word_seconds: float | None = (
            None  # in the latest model call that wrote any text
        )
        # (provider, model asked for, model the provider says answered), for the latest reply
        self.last_reply: tuple[str, str, str] | None = None

        # Tools.
        self.tool_calls: Counter[str] = (
            Counter()
        )  # tool name -> how many times it was called
        self.files_changed: set[Path] = set()
        self.tool_errors = 0  # results that were errors; a "no" from you isn't counted

        # The turn in progress, if there is one.
        self._turn_started: float | None = None
        self._tokens_at_turn_start = 0
        self.calls_this_turn = 0  # model calls finished in this turn
        self.waiting_for_model = False  # a model call is in progress
        self._call_started: float | None = None  # ...and hasn't written any text yet
        self.running_tool: ToolUseBlock | None = None

    # --- What the front end tells us ------------------------------------------

    def start_turn(self) -> None:
        now = self.clock()
        self.turns += 1
        self._turn_started = now
        self._tokens_at_turn_start = self.total_tokens
        self.calls_this_turn = 0
        self._model_call_starts(now)

    def end_turn(self) -> None:
        """The turn is over, however it ended. Does nothing if no turn was
        running: /compact, for instance, ends the same way but isn't a turn."""
        if self._turn_started is None:
            return
        self.last_turn_seconds = self.clock() - self._turn_started
        self.last_turn_tokens = self.total_tokens - self._tokens_at_turn_start
        self.finished_turns += 1
        self.finished_turn_tokens += self.last_turn_tokens
        self._turn_started = None
        self.waiting_for_model = False
        self._call_started = None
        self.running_tool = None

    def record(self, event: Any, provider: Provider) -> None:
        """Take note of one event from the agent. `provider` is the one the
        agent is using now: it's what the event's model call was made with,
        since the model can only be changed between turns."""
        if isinstance(event, TextDelta):
            if self._call_started is not None:  # the first words of this call
                self.first_word_seconds = self.clock() - self._call_started
                self._call_started = None

        elif isinstance(event, Completed):
            self._spent(event.usage, provider)
            self.last_reply = (provider.name, provider.model, event.model)
            self.calls_this_turn += 1
            self.waiting_for_model = False
            self._call_started = None

        elif isinstance(event, ToolStarted):
            self.tool_calls[event.call.name] += 1
            self.running_tool = event.call

        elif isinstance(event, ToolFinished):
            self.running_tool = None
            result = event.result
            if result.is_error and result.content != DECLINED:
                self.tool_errors += 1
            if result.change is not None and not result.is_error:
                self.files_changed.add(result.change.path)
            # When the last tool in a batch finishes, the agent calls the
            # model again straight away. If another tool follows instead,
            # ToolStarted will say so in a moment.
            self._model_call_starts(self.clock())

        elif isinstance(event, Compacted):
            self._spent(event.usage, provider)
            if not event.error:
                self.compactions += 1

    def _model_call_starts(self, now: float) -> None:
        self.waiting_for_model = True
        self._call_started = now

    def _spent(self, usage: Usage, provider: Provider) -> None:
        if usage == Usage():
            return  # no call was made (compaction with nothing to compact)
        self.usage += usage
        prices = prices_of(provider)
        cost = prices.cost(usage) if prices else None
        if cost is None:
            self.unpriced_calls += 1
        else:
            self.cost += cost

    # --- What the front end reads back ------------------------------------------

    @property
    def total_tokens(self) -> int:
        """Every token billed: everything sent, however it was billed, and everything written."""
        return self.usage.sent_tokens + self.usage.output_tokens

    @property
    def in_turn(self) -> bool:
        return self._turn_started is not None

    @property
    def step(self) -> int:
        """Which model call of the turn we're on: the number the step
        limit (`Agent.max_steps`) counts."""
        return self.calls_this_turn + (1 if self.waiting_for_model else 0)

    def elapsed(self) -> float:
        """Seconds since Wren opened."""
        return self.clock() - self.opened_at

    def turn_elapsed(self) -> float | None:
        """Seconds the current turn has been running, or None between turns."""
        return None if self._turn_started is None else self.clock() - self._turn_started

    def average_turn_tokens(self) -> int | None:
        if not self.finished_turns:
            return None
        return round(self.finished_turn_tokens / self.finished_turns)

    def cache_hit(self) -> float | None:
        """The share of input that was read from the cache, 0 to 1."""
        sent = self.usage.sent_tokens
        return self.usage.cache_read_tokens / sent if sent else None

    def cost_per_minute(self) -> Decimal | None:
        """US dollars per minute since Wren opened. None in the first minute,
        when a single reply would make the rate look enormous, and when no
        call so far has had a known price."""
        minutes = Decimal(self.elapsed()) / 60
        if minutes < 1 or (self.unpriced_calls and not self.cost):
            return None
        return self.cost / minutes

    def answered_by_another(self, provider: Provider) -> str | None:
        """The model the provider says wrote the latest reply, if it isn't
        the one Wren asked for. Providers often answer with a dated version
        of the name asked for (asked "x", answered "x-2026-09-01"), so a
        name that merely starts with the one asked for counts as the same.
        Says nothing once the model has been changed since that reply."""
        if self.last_reply is None:
            return None
        name, asked, answered = self.last_reply
        if (name, asked) != (provider.name, provider.model) or not answered:
            return None
        return None if answered.startswith(asked) else answered


def prices_of(provider: Provider) -> Prices | None:
    """The provider's prices for its current model, or None if it doesn't
    know them, or has no way to say (a provider written before prices were)."""
    try:
        return provider.prices()
    except Exception:
        return None
