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

There are two ways to pay, chosen when Wren starts (config.AUTH_ROUTES): an
API key, or your ChatGPT plan after `wren login`. Same model, same API,
same translation; the plan route's own rules are in openai_plan.py, and
`plan` below is what switches it on.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from decimal import Decimal
from typing import Any

import openai

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

# Every level the API knows, lowest first. Unlike Anthropic's, OpenAI's API
# has no way to ask which of these a given model accepts (each takes only
# some), so /effort offers them all and an unsupported choice is refused by
# the API on the next request.
EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# Documented defaults, used independently of the last response's effort.
# Copied from OpenAI's reasoning guide, so they can go stale. A model that
# isn't covered is reported as unknown, never guessed. Checked October 2026.
DEFAULT_EFFORT_SOURCE = (
    "per OpenAI's documentation, October 2026; not measured from a response"
)
DOCUMENTED_DEFAULT_EFFORT = {
    "gpt-5.5": "medium",
    "gpt-5.6": "medium",
    "gpt-6.1-sol": "medium",
    "gpt-6-sol": "medium",
    "gpt-6-luna": "medium",
}

# What each model costs, in US dollars per million tokens. No API reports
# prices, so these are copied from OpenAI's pricing page and can go stale: a
# model that isn't listed has its cost shown as unknown, never guessed.
# These are the Standard tier, the one Wren uses, on the normal endpoint
# (regional data-residency endpoints charge 10% more).
PRICES_SOURCE = (
    "https://developers.openai.com/api/docs/pricing, checked 12 October 2026"
)
LONG_PROMPT = 272_000  # "Long context" on the pricing page: more than 272K input tokens


def _usd(
    input: str, cached_input: str, cache_writes: str, output: str, **long_prompt: Any
) -> Prices:
    """A row of the pricing page's table, in the page's column order, so the
    two can be compared line by line."""
    return Prices(
        input=Decimal(input),
        output=Decimal(output),
        cache_read=Decimal(cached_input),
        cache_write=Decimal(cache_writes),
        **long_prompt,
    )


DOCUMENTED_PRICES: dict[str, Prices] = {
    # Short and long context prices both published.
    "gpt-6-astra": _usd(
        "10",
        "1",
        "12.50",
        "50",
        long_prompt_over=LONG_PROMPT,
        long_prompt=_usd("20", "2", "25", "75"),
    ),
    "gpt-6.1-sol": _usd(
        "2",
        "0.10",
        "2.50",
        "10",
        long_prompt_over=LONG_PROMPT,
        long_prompt=_usd("4", "0.20", "5", "15"),
    ),
    "gpt-6-luna": _usd(
        "0.10",
        "0.01",
        "0.125",
        "0.50",
        long_prompt_over=LONG_PROMPT,
        long_prompt=_usd("0.20", "0.02", "0.25", "0.75"),
    ),
    # A promotional price, "available at least through November 21, 2026".
    "gpt-5.6-sol": _usd(
        "4",
        "0.40",
        "5",
        "20",
        long_prompt_over=LONG_PROMPT,
        long_prompt=_usd("8", "0.80", "10", "30"),
    ),
    # One price published, with no long context price.
    "gpt-6-sol": _usd("2", "0.20", "2.50", "10"),
    "gpt-5.6-terra": _usd("2", "0.20", "2.50", "12"),
    "gpt-5.6-luna": _usd("0.20", "0.02", "0.25", "1.20"),
    # Published "for <272K context length" only, so a longer call is unpriced.
    # The page gives no cache-write price ("-"), so if a cache write is ever
    # reported it's charged at the input price: an assumption, not a quote.
    "gpt-5.5": _usd("5", "0.50", "5", "30", long_prompt_over=LONG_PROMPT),
    "gpt-5.4": _usd("2.50", "0.25", "2.50", "15", long_prompt_over=LONG_PROMPT),
}

# Each model's context window: the most tokens one request may hold, its
# reply included. OpenAI's API doesn't report it (a model there is just an
# id, a date and an owner), so these are copied from each model's page in the
# docs and can go stale: a model that isn't listed has its window shown as
# unknown, never guessed. Every page also gives 128,000 max output tokens.
CONTEXT_WINDOW_SOURCE = "per OpenAI's model pages, https://developers.openai.com/api/docs/models, checked 12 October 2026"
DOCUMENTED_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-6-astra": 1_050_000,
    "gpt-6.1-sol": 1_050_000,
    "gpt-6-sol": 1_050_000,
    "gpt-6-luna": 1_050_000,
    "gpt-5.6-sol": 1_050_000,
    "gpt-5.6-terra": 1_050_000,
    "gpt-5.6-luna": 1_050_000,
    "gpt-5.6": 1_050_000,
    "gpt-5.5": 1_050_000,
    "gpt-5.4": 1_050_000,
}

# `models.list()` returns everything on the account: image, speech and
# embedding models too. These fragments mark the ones that can't hold a
# conversation with tools, so the picker can leave them out.
_NOT_CHAT = (
    "embedding",
    "whisper",
    "tts",
    "audio",
    "realtime",
    "transcribe",
    "image",
    "dall-e",
    "sora",
    "moderation",
)


class OpenAIProvider:
    name = "openai"
    default_effort_source = DEFAULT_EFFORT_SOURCE

    def __init__(
        self,
        model: str,
        client: Any = None,
        effort: str | None = None,
        plan: Any = None,
        plan_client: Callable[[str], Any] | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        # `plan`: a signed-in auth.chatgpt.ChatGPTSession, to pay with your
        # ChatGPT plan. None: pay with your API key.
        self.plan = plan
        if plan is None:
            self.billing = "API key"
            # The SDK reads OPENAI_API_KEY from the environment by itself.
            # Tests pass in a fake `client` instead, so no network is needed.
            self.client = client if client is not None else openai.OpenAI()
        else:
            from wren.providers import openai_plan

            self.billing = "ChatGPT plan"
            # No API-key client at all on this route, so nothing in this
            # provider can quietly pay with one. Each request makes its own
            # client, with a fresh access token (tests pass a fake maker).
            self.client = None
            self._plan_client = plan_client or openai_plan.make_client

    @property
    def billing_note(self) -> str:
        """Who pays, in a sentence, for /usage."""
        if self.plan is None:
            return "your OpenAI API key (OPENAI_API_KEY), billed to your API account"
        from wren.providers.openai_plan import USAGE_SETTINGS_URL

        try:
            who = self.plan.credentials().email or "your ChatGPT account"
        except Exception:  # noqa: BLE001 - signed out since: say so, don't fail /usage
            who = "a ChatGPT sign-in that's no longer there"
        return (
            f"your ChatGPT plan, signed in as {who}. It shares your plan's limits with "
            f"ChatGPT and other apps; see {USAGE_SETTINGS_URL}"
        )

    def models(self) -> list[str]:
        if self.plan is not None:
            from wren.providers.openai_plan import list_models

            return list_models(self._plan_client(self.plan.access_token()))
        ids = [model.id for model in self.client.models.list()]
        chat = [
            i
            for i in ids
            if i.startswith(("gpt-", "o")) and not any(part in i for part in _NOT_CHAT)
        ]
        return sorted(chat, reverse=True)  # roughly newest first

    def efforts(self) -> list[str]:
        return list(EFFORT_LEVELS)

    def default_effort(self) -> str | None:
        if self.model in DOCUMENTED_DEFAULT_EFFORT:
            return DOCUMENTED_DEFAULT_EFFORT[self.model]
        # The guide gives one default for the whole GPT-5.6 family.
        return "medium" if self.model.startswith("gpt-5.6-") else None

    def prices(self) -> Prices | None:
        if self.plan is not None:
            # Plan usage isn't charged per token: it counts against your
            # plan's limits. So there's no price, and cost shows as unknown.
            return None
        return DOCUMENTED_PRICES.get(self.model)

    def context_window(self) -> int | None:
        return DOCUMENTED_CONTEXT_WINDOWS.get(self.model)

    context_window_source = CONTEXT_WINDOW_SOURCE

    def stream(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: Sequence[ToolSpec] = (),
        max_tokens: int,
    ) -> Iterator[StreamEvent]:
        if self.plan is not None:
            yield from self._stream_on_plan(system, messages, tools)
            return
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

        yield _completed(final, request)

    def _stream_on_plan(
        self, system: str, messages: list[Message], tools: Sequence[ToolSpec]
    ) -> Iterator[StreamEvent]:
        from wren.providers import openai_plan

        # A sign-in problem (signed out, plan usage not allowed, a refresh
        # that failed) raises here, before anything is sent: the request
        # stops, and nothing tries another way to pay.
        client = self._plan_client(self.plan.access_token())
        request = openai_plan.build_request(
            model=self.model,
            effort=self.effort,
            system=system,
            messages=messages,
            tools=tools,
        )
        try:
            with client.responses.create(**request) as events:
                final = yield from openai_plan.read_stream(events)
        except openai.APIError as e:
            raise openai_plan.explain(e) from e
        yield _completed(final, request)


def _completed(final: Any, request: dict[str, Any]) -> Completed:
    message = from_response(final)
    return Completed(
        message=message,
        stop_reason=_stop_reason(final, message),
        usage=_usage(final.usage),
        model=final.model,  # from the response, not from self.model
        effort_sent=request.get("reasoning", {}).get("effort"),
        # Response metadata, not a fallback to the requested setting.
        effort_reported=getattr(getattr(final, "reasoning", None), "effort", None),
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
        return {
            "type": "function_call",
            "call_id": block.id,
            "name": block.name,
            "arguments": json.dumps(block.input),
        }
    if isinstance(block, ToolResultBlock):
        # There's no "is_error" here. An error is just text the model reads,
        # and Wren's error results already say what went wrong.
        return {
            "type": "function_call_output",
            "call_id": block.tool_use_id,
            "output": block.content,
        }
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
            blocks.append(
                ToolUseBlock(
                    id=item.call_id, name=item.name, input=_arguments(item.arguments)
                )
            )
        elif item.type == "reasoning":
            # Kept whole, in its place, to be sent back unchanged (see
            # messages.ReasoningBlock). `model_dump` turns the SDK's object
            # back into the plain dict the API sent us.
            blocks.append(
                ReasoningBlock(
                    OpenAIProvider.name, item.model_dump(mode="json", exclude_none=True)
                )
            )
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
        thinking_tokens=getattr(
            api_usage.output_tokens_details, "reasoning_tokens", None
        )
        or 0,
    )
