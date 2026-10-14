"""The Claude provider: translates Wren's neutral types to and from the
Anthropic Messages API, and turns its stream into Wren stream events.

Two pure translation functions (`to_api_messages`, `from_api_message`) do the
format conversion. Keeping them as plain functions, separate from the network
code, means they can be unit-tested without an API key.
"""

from __future__ import annotations

from collections.abc import Generator, Sequence
from decimal import Decimal
from typing import Any

import anthropic

from wren.messages import (
    Block,
    Completed,
    Message,
    Prices,
    ReasoningBlock,
    StopReason,
    StreamEvent,
    TextBlock,
    TextDelta,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")  # lowest first

# What each model does when no effort is sent. The API does not report
# this, for any model, so it's copied from Anthropic's documentation and can
# go stale: a model that isn't listed is reported as "unknown", never
# guessed. Checked October 2026.
DEFAULT_EFFORT_SOURCE = (
    "per Anthropic's documentation, October 2026; the API doesn't report it"
)
DOCUMENTED_DEFAULT_EFFORT = {
    "claude-opus-5-5": "medium",
    "claude-haiku-5-5": "medium",
    "claude-fable-5-1": "high",
    "claude-fable-5": "high",
    "claude-opus-5": "high",
    "claude-opus-4-8": "high",
    "claude-opus-4-7": "high",
    "claude-opus-4-6": "high",
    "claude-sonnet-5-5": "high",
    "claude-sonnet-5": "high",
    "claude-sonnet-4-6": "high",
}

# What each model costs, in US dollars per million tokens. The API doesn't
# report prices either, so these are copied from Anthropic's pricing page and
# can go stale: a model that isn't listed has its cost shown as unknown,
# never guessed. Cache writes are priced for the 5-minute cache, the one
# Wren asks for (see `stream`). Wren uses neither of the options that change
# these (US-only inference, fast mode).
PRICES_SOURCE = (
    "https://platform.claude.com/docs/en/about-claude/pricing, checked 12 October 2026"
)


def _usd(
    base_input: str,
    cache_write_5m: str,
    cache_hit: str,
    output: str,
    **long_prompt: Any,
) -> Prices:
    """A row of the pricing page's table, in the page's column order, so the
    two can be compared line by line."""
    return Prices(
        input=Decimal(base_input),
        output=Decimal(output),
        cache_read=Decimal(cache_hit),
        cache_write=Decimal(cache_write_5m),
        **long_prompt,
    )


DOCUMENTED_PRICES: dict[str, Prices] = {
    "claude-fable-5-1": _usd("10", "12.50", "0.25", "50"),
    "claude-fable-5": _usd("10", "12.50", "1", "50"),
    "claude-opus-5-5": _usd("4", "5", "0.20", "20"),
    "claude-opus-5": _usd("5", "6.25", "0.50", "25"),
    "claude-opus-4-8": _usd("5", "6.25", "0.50", "25"),
    "claude-opus-4-7": _usd("5", "6.25", "0.50", "25"),
    "claude-opus-4-6": _usd("5", "6.25", "0.50", "25"),
    "claude-sonnet-5-5": _usd("2", "2.50", "0.10", "10"),
    "claude-sonnet-5": _usd("2", "2.50", "0.20", "10"),
    "claude-sonnet-4-6": _usd("3", "3.75", "0.30", "15"),
    # Priced by prompt length: over 100,000 tokens, every token costs more.
    "claude-haiku-5-5": _usd(
        "0.10",
        "0.125",
        "0.01",
        "0.50",
        long_prompt_over=100_000,
        long_prompt=_usd("0.50", "0.625", "0.05", "2.50"),
    ),
}

_KNOWN_STOP_REASONS = {"end_turn", "max_tokens", "tool_use", "stop_sequence", "refusal"}


class AnthropicProvider:
    name = "anthropic"
    billing = "API key"

    def __init__(
        self, model: str, client: Any = None, effort: str | None = None
    ) -> None:
        self.model = model
        self.effort = effort
        # The SDK reads ANTHROPIC_API_KEY from the environment by itself.
        # Tests pass in a fake `client` instead, so no network is needed.
        self.client = client if client is not None else anthropic.Anthropic()

    def efforts(self) -> list[str]:
        # Which levels a model takes varies by model (older ones take none),
        # so we ask the API about this one. It's reported per level.
        capabilities = self.client.models.retrieve(self.model).capabilities
        effort = getattr(capabilities, "effort", None)
        if effort is None or not effort.supported:
            return []
        return [
            level
            for level in EFFORT_LEVELS
            if getattr(getattr(effort, level, None), "supported", False)
        ]

    def default_effort(self) -> str | None:
        return DOCUMENTED_DEFAULT_EFFORT.get(self.model)

    default_effort_source = DEFAULT_EFFORT_SOURCE

    def prices(self) -> Prices | None:
        return DOCUMENTED_PRICES.get(self.model)

    def context_window(self) -> int | None:
        # Reported by the API for each model, so there's no table here to go
        # stale. (The agent asks once per model, not on every request.)
        return getattr(
            self.client.models.retrieve(self.model), "max_input_tokens", None
        )

    context_window_source = "as reported by the Anthropic API"

    def models(self) -> list[str]:
        # Asked of the API, not written down here: a list in the code would
        # be out of date within months. The SDK pages through the results;
        # they arrive newest first.
        return [model.id for model in self.client.models.list()]

    def stream(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: Sequence[ToolSpec] = (),
        max_tokens: int,
    ) -> Generator[StreamEvent, None, None]:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": to_api_messages(messages),
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = to_api_tools(tools)
        # Prompt caching. Every request re-sends the whole conversation, and
        # all but the newest part is identical to last time. This one field
        # asks the API to remember what it has processed up to the end of
        # this request, so the next request's matching beginning is read
        # back at about a tenth of the price.
        #
        # It works because requests are built the same way every time: tools,
        # then the system prompt, then messages in order, nothing reshuffled.
        # A cache is matched from the start, byte for byte, so anything that
        # changed near the front would throw away everything after it.
        request["cache_control"] = {"type": "ephemeral"}
        if self.effort:
            # Left out entirely when unset, so the model uses its own
            # default (which differs between models).
            request["output_config"] = {"effort": self.effort}

        # `messages.stream` keeps an HTTP connection open and receives the reply
        # as a series of server-sent events. The SDK's `text_stream` gives us
        # just the text fragments; `get_final_message` gives the assembled
        # message once the stream ends. The `with` block guarantees the
        # connection is closed even if we stop early (e.g. on Ctrl-C).
        # While the model is writing a tool request, no text arrives: the
        # request's JSON is assembled by the SDK and appears in the final
        # message as a tool_use block.
        with self.client.messages.stream(**request) as stream:
            for text in stream.text_stream:
                yield TextDelta(text)
            final = stream.get_final_message()

        yield Completed(
            message=from_api_message(final),
            stop_reason=_stop_reason(final.stop_reason),
            usage=_usage(final.usage),
            model=final.model,  # from the response, not from self.model
            # Read back from the request we built, so it's what was really
            # sent. The response has no field saying what effort was used.
            effort_sent=request.get("output_config", {}).get("effort"),
        )


def _usage(api_usage: Any) -> Usage:
    return Usage(
        input_tokens=api_usage.input_tokens,
        output_tokens=api_usage.output_tokens,
        # None when caching played no part in the request.
        cache_read_tokens=getattr(api_usage, "cache_read_input_tokens", None) or 0,
        cache_write_tokens=getattr(api_usage, "cache_creation_input_tokens", None) or 0,
        thinking_tokens=getattr(
            getattr(api_usage, "output_tokens_details", None), "thinking_tokens", None
        )
        or 0,
    )


# --- Translation: Wren -> Anthropic ---------------------------------------


def to_api_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]


def to_api_messages(messages: list[Message]) -> list[dict[str, Any]]:
    api_messages = []
    for m in messages:
        content = [api for api in map(_block_to_api, m.content) if api is not None]
        api_messages.append({"role": m.role, "content": content})
    return api_messages


def _block_to_api(block: Block) -> dict[str, Any] | None:
    if isinstance(block, ReasoningBlock):
        # Ours go back exactly as they came. Another provider's reasoning
        # would mean nothing to Claude, so it's left out.
        return block.data if block.provider == AnthropicProvider.name else None
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    raise TypeError(f"unknown block type: {type(block).__name__}")


# --- Translation: Anthropic -> Wren ---------------------------------------


def from_api_message(api_message: Any) -> Message:
    blocks: list[Block] = []
    for b in api_message.content:
        if b.type == "text":
            blocks.append(TextBlock(b.text))
        elif b.type == "tool_use":
            blocks.append(ToolUseBlock(id=b.id, name=b.name, input=dict(b.input)))
        elif b.type in ("thinking", "redacted_thinking"):
            # Kept whole, in its place among the other blocks, to be sent
            # back unchanged (see ReasoningBlock). `model_dump` turns the
            # SDK's object back into the plain dict the API sent us.
            blocks.append(
                ReasoningBlock(
                    AnthropicProvider.name, b.model_dump(mode="json", exclude_none=True)
                )
            )
        # Any other block type is something Wren doesn't use yet, and is dropped.
    return Message("assistant", blocks)


def _stop_reason(raw: str | None) -> StopReason:
    return raw if raw in _KNOWN_STOP_REASONS else "other"  # type: ignore[return-value]
