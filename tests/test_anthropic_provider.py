"""Tests for the Claude provider that need no network and no API key.

We check the two translation functions against the SDK's real types, and
drive `stream` with a tiny stand-in for the SDK client.
"""

from anthropic.types import Message as ApiMessage

from wren.messages import (
    ReasoningBlock,
    Completed,
    Message,
    TextBlock,
    TextDelta,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from wren.providers.anthropic import AnthropicProvider, from_api_message, to_api_messages


def api_message(content, stop_reason="end_turn"):
    return ApiMessage.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5-5",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 3},
        }
    )


def test_to_api_messages_handles_every_block_type():
    messages = [
        Message.user("list files"),
        Message("assistant", [TextBlock("Sure."), ToolUseBlock("t1", "ls", {"path": "."})]),
        Message("user", [ToolResultBlock("t1", "a.py\nb.py")]),
    ]
    assert to_api_messages(messages) == [
        {"role": "user", "content": [{"type": "text", "text": "list files"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Sure."},
                {"type": "tool_use", "id": "t1", "name": "ls", "input": {"path": "."}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "a.py\nb.py", "is_error": False}
            ],
        },
    ]


def test_from_api_message_reads_text_and_tool_use():
    msg = from_api_message(
        api_message(
            [
                {"type": "text", "text": "Let me look."},
                {"type": "tool_use", "id": "t1", "name": "ls", "input": {"path": "."}},
            ],
            stop_reason="tool_use",
        )
    )
    assert msg == Message("assistant", [TextBlock("Let me look."), ToolUseBlock("t1", "ls", {"path": "."})])


# --- A stand-in for the SDK client -----------------------------------------


class FakeStream:
    def __init__(self, chunks, final):
        self.text_stream = iter(chunks)
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._final


class FakeClient:
    def __init__(self, chunks, final):
        self.requests = []
        self.messages = self  # so `client.messages.stream(...)` works
        self._stream = FakeStream(chunks, final)

    def stream(self, **request):
        self.requests.append(request)
        return self._stream


def test_stream_yields_deltas_then_completed():
    client = FakeClient(["Hel", "lo"], api_message([{"type": "text", "text": "Hello"}]))
    provider = AnthropicProvider("claude-sonnet-5-5", client=client)

    events = list(provider.stream(system="be nice", messages=[Message.user("hi")], max_tokens=50))

    assert events[:2] == [TextDelta("Hel"), TextDelta("lo")]
    done = events[2]
    assert isinstance(done, Completed)
    assert done.message.text == "Hello"
    assert done.stop_reason == "end_turn"
    assert (done.usage.input_tokens, done.usage.output_tokens) == (12, 3)

    request = client.requests[0]
    assert request["model"] == "claude-sonnet-5-5"
    assert request["max_tokens"] == 50
    assert request["system"] == "be nice"


def test_empty_system_prompt_and_no_tools_are_left_out_of_the_request():
    client = FakeClient([], api_message([{"type": "text", "text": ""}]))
    provider = AnthropicProvider("m", client=client)
    list(provider.stream(system="", messages=[Message.user("hi")], max_tokens=10))
    assert "system" not in client.requests[0]
    assert "tools" not in client.requests[0]


def test_tools_are_sent_in_anthropic_format_and_tool_requests_come_back():
    final = api_message(
        [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.py"}}],
        stop_reason="tool_use",
    )
    client = FakeClient([], final)
    provider = AnthropicProvider("m", client=client)
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    events = list(
        provider.stream(
            system="",
            messages=[Message.user("read a.py")],
            tools=[ToolSpec("read_file", "Read a file.", schema)],
            max_tokens=10,
        )
    )

    assert client.requests[0]["tools"] == [{"name": "read_file", "description": "Read a file.", "input_schema": schema}]
    done = events[-1]
    assert done.stop_reason == "tool_use"
    assert done.message.tool_uses == [ToolUseBlock("t1", "read_file", {"path": "a.py"})]


# --- Reasoning blocks and caching ---------------------------------------------------------


def test_thinking_blocks_come_back_sealed_and_go_out_exactly_as_they_came():
    thinking = {"type": "thinking", "thinking": "", "signature": "sig-1"}
    redacted = {"type": "redacted_thinking", "data": "opaque"}
    message = from_api_message(api_message([thinking, {"type": "text", "text": "Hi"}, redacted]))

    assert [type(b).__name__ for b in message.content] == ["ReasoningBlock", "TextBlock", "ReasoningBlock"]
    sent = to_api_messages([message])[0]["content"]
    assert sent == [thinking, {"type": "text", "text": "Hi"}, redacted]  # same data, same order


def test_another_providers_reasoning_is_not_sent_to_claude():
    foreign = Message("assistant", [ReasoningBlock("other", {"x": 1}), TextBlock("Hi")])
    assert to_api_messages([foreign])[0]["content"] == [{"type": "text", "text": "Hi"}]


def test_requests_ask_for_caching_and_cached_tokens_are_counted():
    final = api_message([{"type": "text", "text": "Hi"}])
    final.usage.cache_read_input_tokens = 900
    final.usage.cache_creation_input_tokens = 40
    client = FakeClient([], final)
    done = list(AnthropicProvider("m", client=client).stream(system="s", messages=[Message.user("hi")], max_tokens=5))[-1]

    assert client.requests[0]["cache_control"] == {"type": "ephemeral"}
    assert (done.usage.cache_read_tokens, done.usage.cache_write_tokens) == (900, 40)
    assert done.usage.sent_tokens == 12 + 900 + 40
