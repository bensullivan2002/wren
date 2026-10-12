"""Tests for the OpenAI provider. The translation functions are pure, so
they're tested directly; the network is replaced by a fake client that
records the request and plays back a response built from the SDK's own types."""

import json
from types import SimpleNamespace

import pytest
from openai.types.responses import Response

from wren.agent import Agent
from wren.commands import run_command
from wren.messages import (
    Completed,
    Message,
    ReasoningBlock,
    TextBlock,
    TextDelta,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    history_problems,
)
from wren.providers import make_provider
from wren.providers.openai import EFFORT_LEVELS, OpenAIProvider, from_response, to_api_tools, to_input_items
from wren.tools import Tool, ToolInput, ToolRegistry, ToolResult

REASONING = {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "sealed-bytes"}
CALL = {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "shout", "arguments": '{"text": "hi"}', "status": "completed"}


def text_item(text):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def response(output, status="completed", incomplete=None, usage=None, model="gpt-6-astra", reasoning=None):
    return Response.model_validate(
        {
            "id": "resp_1",
            "created_at": 0,
            "model": model,
            "object": "response",
            "output": output,
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "status": status,
            "incomplete_details": {"reason": incomplete} if incomplete else None,
            "reasoning": reasoning,
            "usage": usage
            or {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens": 20,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 120,
            },
        }
    )


class FakeStream:
    def __init__(self, chunks, final):
        self._events = [SimpleNamespace(type="response.created")]
        self._events += [SimpleNamespace(type="response.output_text.delta", delta=chunk) for chunk in chunks]
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_response(self):
        return self._final


class FakeClient:
    """Plays back one response per request, and records the requests."""

    def __init__(self, *finals, chunks=()):
        self.requests = []
        self._finals = list(finals)
        self._chunks = list(chunks)
        self.responses = SimpleNamespace(stream=self._stream)
        ids = ["gpt-6-astra", "gpt-5.6-terra", "text-embedding-3-large", "gpt-image-2", "o9-mini", "whisper-1"]
        self.models = SimpleNamespace(list=lambda: [SimpleNamespace(id=i) for i in ids])

    def _stream(self, **request):
        self.requests.append(json.loads(json.dumps(request)))  # a copy, and proof it's all plain JSON
        return FakeStream(self._chunks, self._finals.pop(0))


# --- Wren -> OpenAI ------------------------------------------------------------------


def test_messages_are_flattened_into_items_in_order():
    sealed = ReasoningBlock("openai", REASONING)
    history = [
        Message.user("shout hi"),
        Message("assistant", [sealed, TextBlock("On it."), ToolUseBlock("call_1", "shout", {"text": "hi"})]),
        Message("user", [ToolResultBlock("call_1", "HI"), ToolResultBlock("call_2", "boom", is_error=True)]),
        Message.assistant("Done."),
    ]
    assert to_input_items(history) == [
        {"role": "user", "content": "shout hi"},
        REASONING,  # exactly as it came, and before the items it led to
        {"role": "assistant", "content": "On it."},
        {"type": "function_call", "call_id": "call_1", "name": "shout", "arguments": '{"text": "hi"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "HI"},
        {"type": "function_call_output", "call_id": "call_2", "output": "boom"},
        {"role": "assistant", "content": "Done."},
    ]


def test_another_providers_reasoning_is_not_sent_to_openai():
    foreign = Message("assistant", [ReasoningBlock("anthropic", {"type": "thinking"}), TextBlock("Hi")])
    assert to_input_items([foreign]) == [{"role": "assistant", "content": "Hi"}]


def test_tools_are_declared_as_functions_and_explicitly_not_strict():
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    assert to_api_tools([ToolSpec("read_file", "Read a file.", schema)]) == [
        {"type": "function", "name": "read_file", "description": "Read a file.", "parameters": schema, "strict": False}
    ]


# --- OpenAI -> Wren ------------------------------------------------------------------


def test_a_responses_items_become_one_assistant_message_in_order():
    message = from_response(response([REASONING, text_item("On it."), CALL]))
    assert [type(b).__name__ for b in message.content] == ["ReasoningBlock", "TextBlock", "ToolUseBlock"]
    assert message.content[0].provider == "openai" and message.content[0].data["encrypted_content"] == "sealed-bytes"
    assert message.text == "On it."
    assert message.tool_uses == [ToolUseBlock("call_1", "shout", {"text": "hi"})]  # arguments parsed from the string


def test_reasoning_survives_a_round_trip_unchanged():
    message = from_response(response([REASONING, CALL]))
    assert to_input_items([message])[0] == {**REASONING}


def test_arguments_that_are_not_a_json_object_become_something_validation_will_refuse():
    for raw in ('{"text": "hi', "[1, 2]", "null"):
        broken = {**CALL, "arguments": raw}
        assert "invalid_json_arguments" in from_response(response([broken])).tool_uses[0].input
    empty = {**CALL, "arguments": ""}
    assert from_response(response([empty])).tool_uses[0].input == {}


# --- A whole call ----------------------------------------------------------------------


def stream(provider, **kwargs):
    kwargs = {"system": "be nice", "messages": [Message.user("hi")], "max_tokens": 50, **kwargs}
    return list(provider.stream(**kwargs))


def test_stream_yields_deltas_then_completed_and_builds_a_stateless_request():
    usage = {
        "input_tokens": 1000,
        "input_tokens_details": {"cached_tokens": 900, "cache_write_tokens": 40},
        "output_tokens": 300,
        "output_tokens_details": {"reasoning_tokens": 250},
        "total_tokens": 1300,
    }
    client = FakeClient(response([text_item("Hello")], usage=usage), chunks=["Hel", "lo"])
    events = stream(OpenAIProvider("gpt-5.6-terra", client=client))

    assert events[:2] == [TextDelta("Hel"), TextDelta("lo")]
    done = events[-1]
    assert isinstance(done, Completed) and done.message.text == "Hello" and done.stop_reason == "end_turn"
    assert done.model == "gpt-6-astra"  # what the response reported, not what we asked for
    # OpenAI's input count includes cached tokens; Wren's is the uncached part.
    # The details are a breakdown of that total, so the three parts add back up to it.
    assert (done.usage.input_tokens, done.usage.cache_read_tokens, done.usage.cache_write_tokens) == (60, 900, 40)
    assert done.usage.sent_tokens == 1000
    assert (done.usage.output_tokens, done.usage.thinking_tokens) == (300, 250)

    request = client.requests[0]
    assert request["model"] == "gpt-5.6-terra" and request["instructions"] == "be nice"
    assert request["max_output_tokens"] == 50
    assert request["store"] is False and request["include"] == ["reasoning.encrypted_content"]
    assert "tools" not in request and "reasoning" not in request and "cache_control" not in request


def test_effort_is_sent_only_when_set_and_recorded_on_the_reply():
    client = FakeClient(response([text_item("a")]), response([text_item("b")]))
    provider = OpenAIProvider("m", client=client)
    assert stream(provider)[-1].effort_sent is None
    provider.effort = "high"
    assert stream(provider)[-1].effort_sent == "high"
    assert client.requests[1]["reasoning"] == {"effort": "high"}


@pytest.mark.parametrize("sent, reported", [("high", "xhigh"), (None, "xhigh"), ("high", "none")])
def test_effort_reported_is_read_from_the_response_not_the_request(sent, reported):
    client = FakeClient(response([text_item("hi")], reasoning={"effort": reported}))
    done = stream(OpenAIProvider("m", client=client, effort=sent))[-1]
    assert done.effort_sent == sent
    assert done.effort_reported == reported


@pytest.mark.parametrize("reasoning", [None, {}, {"effort": None}])
def test_missing_response_effort_is_unknown_even_when_effort_was_sent(reasoning):
    client = FakeClient(response([text_item("hi")], reasoning=reasoning))
    done = stream(OpenAIProvider("m", client=client, effort="xhigh"))[-1]
    assert done.effort_sent == "xhigh"
    assert done.effort_reported is None


def test_missing_reasoning_field_is_also_unknown():
    final = response([text_item("hi")])
    del final.reasoning  # a client without this optional response field
    done = stream(OpenAIProvider("m", client=FakeClient(final), effort="xhigh"))[-1]
    assert done.effort_reported is None


def test_usage_shows_response_effort_separately_and_does_not_keep_stale_metadata():
    client = FakeClient(
        response([text_item("first")], reasoning={"effort": "xhigh"}),
        response([text_item("second")]),
    )
    agent = Agent(OpenAIProvider("m", client=client, effort="high"), "sys", 100)
    list(agent.run_turn("hi"))
    shown = run_command(agent, "/usage").text
    assert "Last request was sent with: effort high  (Wren's record)" in shown
    assert "Effort in the last reply: xhigh  (as reported by openai)" in shown
    assert agent.last_call.effort_reported == "xhigh"

    agent.set_effort("low")
    assert "Effort in the last reply: xhigh" in run_command(agent, "/usage").text
    list(agent.run_turn("again"))
    shown = run_command(agent, "/usage").text
    assert "Last request was sent with: effort low  (Wren's record)" in shown
    assert "Effort in the last reply: not reported by openai" in shown
    assert "Effort in the last reply: xhigh" not in shown
    assert history_problems(agent.messages) == []


def test_stop_reasons():
    def stop(final):
        return stream(OpenAIProvider("m", client=FakeClient(final)))[-1].stop_reason

    assert stop(response([text_item("hi")])) == "end_turn"
    assert stop(response([CALL])) == "tool_use"
    assert stop(response([text_item("cut")], status="incomplete", incomplete="max_output_tokens")) == "max_tokens"
    assert stop(response([], status="incomplete", incomplete="content_filter")) == "refusal"
    assert stop(response([], status="failed")) == "other"


def test_models_are_filtered_to_ones_that_can_chat_and_effort_offers_every_level():
    provider = OpenAIProvider("gpt-6-astra", client=FakeClient())
    assert provider.models() == ["o9-mini", "gpt-6-astra", "gpt-5.6-terra"]
    assert provider.efforts() == list(EFFORT_LEVELS) and provider.efforts()[0] == "none"


def test_documented_default_effort_is_given_where_known_and_never_guessed():
    assert OpenAIProvider("gpt-5.6-terra", client=object()).default_effort() == "medium"
    assert OpenAIProvider("gpt-6.1-sol", client=object()).default_effort() == "medium"
    assert OpenAIProvider("gpt-6-astra", client=object()).default_effort() is None  # the guide doesn't say
    assert "documentation" in OpenAIProvider.default_effort_source


def test_the_context_window_comes_from_the_documented_table_and_is_never_guessed():
    from wren.providers.openai import DOCUMENTED_CONTEXT_WINDOWS, DOCUMENTED_PRICES

    assert OpenAIProvider("gpt-6.1-sol", client=object()).context_window() == 1_050_000
    assert OpenAIProvider("gpt-some-future-model", client=object()).context_window() is None
    assert set(DOCUMENTED_PRICES) <= set(DOCUMENTED_CONTEXT_WINDOWS)  # every priced model has a window
    assert "checked" in OpenAIProvider.context_window_source


# --- Through the agent: nothing else in Wren needed to change ------------------------------


class Shout(Tool):
    name = "shout"
    description = "Return the text in capitals."

    class Input(ToolInput):
        text: str

    def run(self, args, ctx):
        return ToolResult(args.text.upper(), summary="shouted")


def test_a_tool_loop_runs_end_to_end_and_reasoning_goes_back_with_the_result():
    client = FakeClient(response([REASONING, CALL]), response([text_item("It said HI.")]))
    agent = Agent(OpenAIProvider("gpt-6-astra", client=client), "sys", 100, ToolRegistry([Shout()]))
    list(agent.run_turn("shout hi"))

    assert agent.messages[-1].text == "It said HI."
    assert history_problems(agent.messages) == []
    second = client.requests[1]["input"]
    assert [item.get("type", "message") for item in second] == [
        "message",  # the user
        "reasoning",  # handed back, sealed
        "function_call",
        "function_call_output",
    ]
    assert second[1]["encrypted_content"] == "sealed-bytes" and second[3]["output"] == "HI"
    assert client.requests[0]["tools"][0]["name"] == "shout"


def test_the_factory_knows_the_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    provider = make_provider("openai", "gpt-5.6-terra", effort="low")
    assert (provider.name, provider.model, provider.effort) == ("openai", "gpt-5.6-terra", "low")
