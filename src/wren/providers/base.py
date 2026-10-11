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

from typing import Iterator, Protocol

from wren.messages import Message, StreamEvent


class Provider(Protocol):
    name: str  # e.g. "anthropic"
    model: str  # e.g. "claude-sonnet-5-5"

    def stream(
        self,
        *,
        system: str,
        messages: list[Message],
        max_tokens: int,
    ) -> Iterator[StreamEvent]:
        """Send the whole conversation to the model; yield events as it replies.

        Contract: yields zero or more TextDelta, then exactly one Completed,
        which is always last. (Step 2 will add a `tools` parameter.)
        """
        ...
