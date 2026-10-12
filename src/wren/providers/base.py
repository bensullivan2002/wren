"""The one interface every model provider implements.

This is the seam between "Wren" and "a model". Anything on the other side of
it (HTTP, SDKs, provider-specific JSON) is the provider's business. Anything
on this side only speaks the neutral types from wren.messages.

`Protocol` means "any class with these attributes and methods counts as a
Provider". Providers don't need to inherit from anything; they just need the
right shape (this is called structural typing, or "duck typing with a type
checker").
"""

from __future__ import annotations

from typing import Iterator, Protocol, Sequence

from wren.messages import Message, StreamEvent, ToolSpec


class Provider(Protocol):
    name: str  # e.g. "anthropic"
    model: str  # e.g. "claude-sonnet-5-5". Read on every request, so it can be changed between them.

    # Effort: how hard the model should work on each reply (how much it
    # thinks, how thorough it is), traded against speed and cost. Providers
    # differ on where this setting lives, and the interface allows for both:
    #
    #   * A SEPARATE SETTING, sent alongside the model's name (Anthropic).
    #     `efforts()` lists the levels the current model takes, and
    #     `effort` holds the chosen one.
    #   * PART OF THE MODEL'S NAME, where a provider offers, say, "x-low"
    #     and "x-high" as different models. Then `efforts()` returns
    #     nothing, the choices appear in `models()`, and `effort` stays None.
    #
    # Either way the rest of Wren does the same thing: ask the provider what
    # there is to choose from, and show that.
    effort: str | None  # None means "the model's own default"

    def efforts(self) -> list[str]:
        """The effort levels the current model accepts as a separate
        setting, lowest first. Empty if it has none. May make a network call."""
        ...

    def models(self) -> list[str]:
        """The models this provider offers right now, for a picker to show.
        May make a network call. Best first, if there's a natural order."""
        ...

    def stream(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: Sequence[ToolSpec] = (),
        max_tokens: int,
    ) -> Iterator[StreamEvent]:
        """Send the whole conversation, and the tools on offer, to the model;
        yield events as it replies.

        Contract: yields zero or more TextDelta, then exactly one Completed,
        which is always last. If the model wants tools, the Completed message
        contains ToolUseBlocks; running them is the agent's job, not ours.
        """
        ...
