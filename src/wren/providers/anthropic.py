"""The Claude provider: translates Wren's neutral types to and from the
Anthropic Messages API, and turns its stream into Wren stream events.

Two pure translation functions (`to_api_messages`, `from_api_message`) do the
format conversion. Keeping them as plain functions, separate from the network
code, means they can be unit-tested without an API key.
"""

from __future__ import annotations

from typing import Any, Iterator

import anthropic

from wren.messages import (
    Block,
    Completed,
    Message,
    StopReason,
    StreamEvent,
    TextBlock,
    TextDelta,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

_KNOWN_STOP_REASONS = {"end_turn", "max_tokens", "tool_use", "stop_sequence", "refusal"}


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, client: Any = None) -> None:
        self.model = model
        # The SDK reads ANTHROPIC_API_KEY from the environment by itself.
        # Tests pass in a fake `client` instead, so no network is needed.
        self.client = client if client is not None else anthropic.Anthropic()

    def stream(
        self,
        *,
        system: str,
        messages: list[Message],
        max_tokens: int,
    ) -> Iterator[StreamEvent]:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": to_api_messages(messages),
        }
        if system:
            request["system"] = system

        # `messages.stream` keeps an HTTP connection open and receives the reply
        # as a series of server-sent events. The SDK's `text_stream` gives us
        # just the text fragments; `get_final_message` gives the assembled
        # message once the stream ends. The `with` block guarantees the
        # connection is closed even if we stop early (e.g. on Ctrl-C).
        with self.client.messages.stream(**request) as stream:
            for text in stream.text_stream:
                yield TextDelta(text)
            final = stream.get_final_message()

        yield Completed(
            message=from_api_message(final),
            stop_reason=_stop_reason(final.stop_reason),
            usage=Usage(final.usage.input_tokens, final.usage.output_tokens),
        )


# --- Translation: Wren -> Anthropic ---------------------------------------


def to_api_messages(messages: list[Message]) -> list[dict[str, Any]]:
    return [
        {"role": m.role, "content": [_block_to_api(b) for b in m.content]}
        for m in messages
    ]


def _block_to_api(block: Block) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
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
        # Other block types (e.g. "thinking") are ignored for now. If we turn on
        # extended thinking later, we'll need to keep them: the API requires
        # thinking blocks to be sent back unchanged during tool use.
    return Message("assistant", blocks)


def _stop_reason(raw: str | None) -> StopReason:
    return raw if raw in _KNOWN_STOP_REASONS else "other"  # type: ignore[return-value]
