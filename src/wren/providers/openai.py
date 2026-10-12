"""The OpenAI provider: translates Wren's neutral types to and from OpenAI's
Responses API.

This is Wren's second real provider, and the test of a claim made in step 1:
that keeping Wren's own message types would make the model swappable. Nothing
outside this file changed to make GPT models work. What's in it is worth
reading beside providers/anthropic.py, because the two APIs describe the
same conversation in different shapes.

    Claude (Messages API)              OpenAI (Responses API)
    --------------------------------   ----------------------------------------
    a list of MESSAGES, each a role    a flat list of ITEMS, of several kinds
    and a list of content blocks

    assistant message containing a     a "function_call" item, on its own
    tool_use block

    user message containing a          a "function_call_output" item, on its
    tool_result block                  own, belonging to nobody

    thinking blocks inside the         "reasoning" items in the list, before
    assistant message                  the items they led to

    system prompt: `system`            system prompt: `instructions`
    tool arguments: a JSON object      tool arguments: a STRING containing JSON
    asks for caching explicitly        caches by itself

So `to_input_items` flattens Wren's messages into items, and
`from_response` gathers one response's items back into a single assistant
message. Order is kept both ways, which matters: a reasoning item has to be
followed by the item it produced.

We keep the conversation ourselves and send all of it each time
(`store=False`), as with Claude. OpenAI can also keep it on its servers and
take just an id; that would make this provider work differently from every
other part of Wren, which assumes the history lives in `agent.messages`.
"""

from __future__ import annotations

import json
from typing import Any, Iterator, Sequence

import openai

from wren.messages import (
    Block,
    Completed,
    Message,
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

# Every level the API knows, lowest first. Unlike Anthropic's, OpenAI's API
# has no way to ask which of these a given model accepts (each takes only
# some), so /effort offers them all and an unsupported choice is refused by
# the API on the next request.
EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# What a model does when no effort is sent. Not reported by the API: copied
# from OpenAI's reasoning guide, so it can go stale. A model that isn't
# covered is reported as unknown, never guessed. Checked October 2026.
DEFAULT_EFFORT_SOURCE = "per OpenAI's documentation, October 2026; the API doesn't report it"
DOCUMENTED_DEFAULT_EFFORT = {
    "gpt-5.5": "medium",
    "gpt-5.6": "medium",
    "gpt-6.1-sol": "medium",
    "gpt-6-sol": "medium",
    "gpt-6-luna": "medium",
}

# `models.list()` returns everything on the account: image, speech and
# embedding models too. These fragments mark the ones that can't hold a
# conversation with tools, so the picker can leave them out.
_NOT_CHAT = ("embedding", "whisper", "tts", "audio", "realtime", "transcribe", "image", "dall-e", "sora", "moderation")


class OpenAIProvider:
    name = "openai"
    default_effort_source = DEFAULT_EFFORT_SOURCE

    def __init__(self, model: str, client: Any = None, effort: str | None = None) -> None:
        self.model = model
        self.effort = effort
        # The SDK reads OPENAI_API_KEY from the environment by itself.
        # Tests pass in a fake `client` instead, so no network is needed.
        self.client = client if client is not None else openai.OpenAI()

    def models(self) -> list[str]:
        ids = [model.id for model in self.client.models.list()]
        chat = [i for i in ids if i.startswith(("gpt-", "o")) and not any(part in i for part in _NOT_CHAT)]
        return sorted(chat, reverse=True)  # roughly newest first

    def efforts(self) -> list[str]:
        return list(EFFORT_LEVELS)

    def default_effort(self) -> str | None:
        if self.model in DOCUMENTED_DEFAULT_EFFORT:
            return DOCUMENTED_DEFAULT_EFFORT[self.model]
        # The guide gives one default for the whole GPT-5.6 family.
        return "medium" if self.model.startswith("gpt-5.6-") else None

    def stream(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: Sequence[ToolSpec] = (),
        max_tokens: int,
    ) -> Iterator[StreamEvent]:
        request: dict[str, Any] = {
            "model": self.model,
            "input": to_input_items(messages),
            "max_output_tokens": max_tokens,
            # We hold the history; don't keep a copy on OpenAI's servers.
            "store": False,
            # ...which means the model's reasoning has to travel with us.
            # This asks for it in a form we can send back: encrypted, so we
            # can carry it without being able to read it.
            "include": ["reasoning.encrypted_content"],
        }
        if system:
            request["instructions"] = system
        if tools:
            request["tools"] = to_api_tools(tools)
        if self.effort:
            # Left out when unset, so the model uses its own default.
            request["reasoning"] = {"effort": self.effort}
        # No caching field: OpenAI caches a repeated beginning by itself.
        # The same rule makes it work, though: the request must start the
        # same way every time (step 5).

        with self.client.responses.stream(**request) as stream:
            for event in stream:
                if event.type == "response.output_text.delta":
                    yield TextDelta(event.delta)
            final = stream.get_final_response()

        message = from_response(final)
        yield Completed(
            message=message,
            stop_reason=_stop_reason(final, message),
            usage=_usage(final.usage),
            model=final.model,  # from the response, not from self.model
            effort_sent=request.get("reasoning", {}).get("effort"),
        )


# --- Translation: Wren -> OpenAI ----------------------------------------------


def to_api_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": t.name,
            "description": t.description,
            "parameters": t.input_schema,
            # Said outright, because leaving it out is not neutral. OpenAI's
            # "strict" mode requires every argument to be listed as required
            # (optional ones become "may be null"), and when the field is
            # absent the API tries to convert the schema to that form. Our
            # tools have real optional arguments with defaults, and reject
            # null, so a converted schema would make the model send
            # arguments that Wren then refuses.
            "strict": False,
        }
        for t in tools
    ]


def to_input_items(messages: list[Message]) -> list[dict[str, Any]]:
    """Flatten Wren's messages into the Responses API's list of items."""
    items: list[dict[str, Any]] = []
    for message in messages:
        for block in message.content:
            item = _block_to_item(block, message.role)
            if item is not None:
                items.append(item)
    return items


def _block_to_item(block: Block, role: str) -> dict[str, Any] | None:
    if isinstance(block, TextBlock):
        return {"role": role, "content": block.text}
    if isinstance(block, ToolUseBlock):
        # Arguments go back as they came: a string containing JSON.
        return {"type": "function_call", "call_id": block.id, "name": block.name, "arguments": json.dumps(block.input)}
    if isinstance(block, ToolResultBlock):
        # There's no "is_error" here. An error is just text the model reads,
        # and Wren's error results already say what went wrong.
        return {"type": "function_call_output", "call_id": block.tool_use_id, "output": block.content}
    if isinstance(block, ReasoningBlock):
        # Ours go back exactly as they came. Another provider's reasoning
        # would mean nothing to this one, so it's left out.
        return block.data if block.provider == OpenAIProvider.name else None
    raise TypeError(f"unknown block type: {type(block).__name__}")


# --- Translation: OpenAI -> Wren ----------------------------------------------


def from_response(response: Any) -> Message:
    """Gather a response's items into one assistant message, in order."""
    blocks: list[Block] = []
    for item in response.output:
        if item.type == "message":
            for part in item.content:
                if part.type == "output_text" and part.text:
                    blocks.append(TextBlock(part.text))
                elif part.type == "refusal":
                    blocks.append(TextBlock(part.refusal))
        elif item.type == "function_call":
            blocks.append(ToolUseBlock(id=item.call_id, name=item.name, input=_arguments(item.arguments)))
        elif item.type == "reasoning":
            # Kept whole, in its place, to be sent back unchanged (see
            # messages.ReasoningBlock). `model_dump` turns the SDK's object
            # back into the plain dict the API sent us.
            blocks.append(ReasoningBlock(OpenAIProvider.name, item.model_dump(mode="json", exclude_none=True)))
        # Any other item type is something Wren doesn't use, and is dropped.
    return Message("assistant", blocks)


def _arguments(raw: str) -> dict[str, Any]:
    """Tool arguments arrive as a string that should contain a JSON object.
    If it doesn't (a reply cut off part-way, say), hand the tool something
    its validation will refuse, with the text in it, so the model is told
    what it sent and can try again."""
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"invalid_json_arguments": raw[:200]}
    return parsed if isinstance(parsed, dict) else {"invalid_json_arguments": raw[:200]}


def _stop_reason(response: Any, message: Message) -> StopReason:
    # There's no single "why it stopped" field to copy. A finished response
    # that asks for tools is "tool_use"; one cut short says why.
    if response.status == "incomplete":
        reason = getattr(response.incomplete_details, "reason", None)
        if reason == "max_output_tokens":
            return "max_tokens"
        return "refusal" if reason == "content_filter" else "other"
    if response.status not in ("completed", None):
        return "other"
    return "tool_use" if message.tool_uses else "end_turn"


def _usage(api_usage: Any) -> Usage:
    if api_usage is None:
        return Usage()
    cached = getattr(api_usage.input_tokens_details, "cached_tokens", None) or 0
    written = getattr(api_usage.input_tokens_details, "cache_write_tokens", None) or 0
    return Usage(
        # OpenAI's input_tokens is the total, cached ones included. Wren's
        # is the uncached part (see messages.Usage), so take them out.
        input_tokens=max(api_usage.input_tokens - cached - written, 0),
        output_tokens=api_usage.output_tokens,
        cache_read_tokens=cached,
        cache_write_tokens=written,
        thinking_tokens=getattr(api_usage.output_tokens_details, "reasoning_tokens", None) or 0,
    )
