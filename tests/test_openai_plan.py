"""Tests for the ChatGPT plan route (providers/openai_plan.py, and the parts
of OpenAIProvider, commands and the CLI that choose it). No network: the
sign-in is a fake that hands out numbered tokens, and the SDK client is a
fake that plays back Responses API events built from the SDK's own types."""

from types import SimpleNamespace

import httpx2
import openai
import pytest
from test_auth import CLIENT, saved
from test_openai_provider import CALL, REASONING, Shout, response, text_item

from wren import cli
from wren.agent import Agent
from wren.auth.chatgpt import SignInRequired
from wren.auth.store import CredentialStore
from wren.commands import provider_choices, run_command, switch_provider
from wren.config import API_KEY, CHATGPT, Config
from wren.messages import (
    Message,
    TextDelta,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    history_problems,
)
from wren.providers import make_provider
from wren.providers.fake import FakeProvider
from wren.providers.openai import OpenAIProvider
from wren.providers.openai_plan import NAMESPACE, PlanRequestError
from wren.tools import ToolRegistry

NAMESPACED_CALL = {**CALL, "namespace": NAMESPACE}


class FakePlan:
    """A signed-in ChatGPT session: a new token for every request."""

    def __init__(self, error=None):
        self.error = error
        self.handed_out = 0

    def access_token(self):
        if self.error:
            raise self.error
        self.handed_out += 1
        return f"tok-{self.handed_out}"

    def credentials(self):
        return SimpleNamespace(email="ben@example.com")


def events(final, chunks=(), end="response.completed"):
    out = [SimpleNamespace(type="response.created")]
    out += [SimpleNamespace(type="response.output_text.delta", delta=c) for c in chunks]
    if end:
        out.append(SimpleNamespace(type=end, response=final))
    return out


class EventStream:
    def __init__(self, items):
        self._items = items

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for item in self._items:
            if isinstance(item, Exception):
                raise item
            yield item


class FakePlanClient:
    """Stands in for openai.OpenAI on the plan route. Each script is the
    list of events (or an exception) for one request. Records the token
    each client was made with, and every request."""

    def __init__(self, *scripts, models=None):
        self.scripts = list(scripts)
        self.models = models
        self.tokens = []
        self.requests = []

    def make(self, token):
        self.tokens.append(token)
        return SimpleNamespace(
            responses=SimpleNamespace(create=self._create), get=self._get
        )

    def _create(self, **request):
        self.requests.append(request)
        script = self.scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        return EventStream(script)

    def _get(self, path, cast_to):
        assert path == "/models" and cast_to is object
        if isinstance(self.models, Exception):
            raise self.models
        return self.models


def plan_provider(client, plan=None, model="gpt-6-luna", effort=None):
    return OpenAIProvider(
        model, effort=effort, plan=plan or FakePlan(), plan_client=client.make
    )


def run(provider, messages=None, tools=()):
    return list(
        provider.stream(
            system="sys",
            messages=messages or [Message.user("hi")],
            tools=tools,
            max_tokens=999,
        )
    )


def http_error(cls, status, body, request_id="req_1"):
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    reply = httpx2.Response(
        status, request=request, headers={"x-request-id": request_id}
    )
    data = body.get("error", body) if isinstance(body, dict) else body
    return cls(f"Error code: {status}", response=reply, body=data)


SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}}


# --- The requests ---------------------------------------------------------------------


def test_requests_follow_the_plan_routes_rules():
    client = FakePlanClient(events(response([text_item("hi")])))
    history = [
        Message.user("shout hi"),
        Message("assistant", [ToolUseBlock("call_1", "shout", {"text": "hi"})]),
        Message("user", [ToolResultBlock("call_1", "HI")]),
    ]
    run(
        plan_provider(client, effort="low"),
        history,
        tools=[ToolSpec("shout", "Shout.", SCHEMA)],
    )
    request = client.requests[0]
    assert request["store"] is False and request["stream"] is True
    assert "max_output_tokens" not in request  # refused on this route
    assert request["instructions"] == "sys" and request["reasoning"] == {
        "effort": "low"
    }
    assert request["include"] == ["reasoning.encrypted_content"]
    [namespace] = request["tools"]
    assert (namespace["type"], namespace["name"]) == ("namespace", "wren")
    assert namespace["tools"] == [
        {
            "type": "function",
            "name": "shout",
            "description": "Shout.",
            "parameters": SCHEMA,
            "strict": False,
        }
    ]
    call = request["input"][1]
    assert call["type"] == "function_call" and call["namespace"] == "wren"
    # No system-role items: the system prompt goes in `instructions`.
    assert all(item.get("role") != "system" for item in request["input"])


def test_a_streamed_turn_and_a_tool_round_trip_run_through_wrens_own_loop():
    client = FakePlanClient(
        events(response([REASONING, NAMESPACED_CALL]), chunks=["Let me ", "shout."]),
        events(response([text_item("It said HI.")]), chunks=["It said ", "HI."]),
    )
    plan = FakePlan()
    agent = Agent(plan_provider(client, plan), "sys", 100, ToolRegistry([Shout()]))
    seen = list(agent.run_turn("shout hi"))

    assert [e.text for e in seen if isinstance(e, TextDelta)] == [
        "Let me ",
        "shout.",
        "It said ",
        "HI.",
    ]
    assert agent.messages[-1].text == "It said HI."
    assert history_problems(agent.messages) == []
    second = client.requests[1]["input"]
    assert [item.get("type", "message") for item in second] == [
        "message",
        "reasoning",  # handed back, sealed
        "function_call",
        "function_call_output",
    ]
    assert second[2]["namespace"] == "wren" and second[3]["output"] == "HI"
    # A fresh token for every request, and never an API key.
    assert client.tokens == ["tok-1", "tok-2"] and agent.provider.client is None


def test_a_second_turn_sends_the_whole_conversation():
    client = FakePlanClient(
        events(response([text_item("One.")])), events(response([text_item("Two.")]))
    )
    agent = Agent(plan_provider(client), "sys", 100)
    list(agent.run_turn("first"))
    list(agent.run_turn("second"))
    assert [item["content"] for item in client.requests[1]["input"]] == [
        "first",
        "One.",
        "second",
    ]
    assert "previous_response_id" not in client.requests[1]
    assert history_problems(agent.messages) == []


def test_an_incomplete_reply_is_a_partial_reply_not_a_failure():
    partial = response(
        [text_item("Half")], status="incomplete", incomplete="content_filter"
    )
    client = FakePlanClient(events(partial, end="response.incomplete"))
    done = run(plan_provider(client))[-1]
    assert done.message.text == "Half" and done.stop_reason == "refusal"


# --- When it goes wrong: the request stops, and nothing else pays ---------------------


def turn_fails(client, plan=None):
    agent = Agent(plan_provider(client, plan), "sys", 100)
    with pytest.raises(Exception) as caught:
        list(agent.run_turn("hi"))
    assert history_problems(agent.messages) == []
    return caught.value, agent


def test_running_out_of_plan_usage_stops_the_turn_and_says_where_to_look():
    limit = http_error(
        openai.RateLimitError,
        429,
        {
            "error": {
                "code": "subscription_sharing_usage_limit_exceeded",
                "message": "limit",
            }
        },
    )
    error, agent = turn_fails(FakePlanClient(limit))
    assert isinstance(error, PlanRequestError) and error.status == 429
    shown = str(error)
    assert "usage limit" in shown and "https://chatgpt.com/settings/usage" in shown
    assert "subscription_sharing_usage_limit_exceeded" in shown and "req_1" in shown
    assert "didn't switch to an API key" in shown
    # The conversation's note carries the first line: what happened.
    assert "usage limit" in agent.messages[-1].text


def test_a_limit_that_arrives_after_text_has_streamed_still_stops_the_turn():
    failed = response([], status="failed")
    failed.error = SimpleNamespace(
        code="subscription_sharing_usage_limit_exceeded", message="x"
    )
    client = FakePlanClient(events(failed, chunks=["Starting"], end="response.failed"))
    error, agent = turn_fails(client)
    assert isinstance(error, PlanRequestError) and "usage limit" in str(error)
    assert "Starting" in agent.messages[-1].text  # what had streamed is kept


def test_an_unsupported_request_names_what_was_refused():
    refused = http_error(
        openai.BadRequestError,
        400,
        {
            "error": {
                "code": "subscription_sharing_unsupported_capability",
                "param": "tools[0]",
            }
        },
    )
    error, _ = turn_fails(FakePlanClient(refused))
    assert "doesn't support (tools[0])" in str(error) and error.param == "tools[0]"


def test_a_refusal_before_the_request_starts_shows_its_detail():
    # Admission can refuse with only {"detail": ...}: no code to go on.
    refused = http_error(
        openai.AuthenticationError, 401, {"detail": "direct use not permitted"}
    )
    error, _ = turn_fails(FakePlanClient(refused))
    shown = str(error)
    assert "didn't accept your sign-in" in shown and "wren login" in shown
    assert "OpenAI said: direct use not permitted" in shown and "HTTP 401" in shown


def test_an_error_event_or_a_stream_that_just_stops_is_a_failure():
    error_event = SimpleNamespace(
        type="error", code="server_error", message="boom", param=None
    )
    error, _ = turn_fails(
        FakePlanClient([SimpleNamespace(type="response.created"), error_event])
    )
    assert "OpenAI said: boom" in str(error)

    cut = events(response([text_item("Hal")]), chunks=["Hal"], end=None)
    error, _ = turn_fails(FakePlanClient(cut))
    assert "stopped before it finished" in str(error)

    dropped = [
        SimpleNamespace(type="response.created"),
        openai.APIConnectionError(
            request=httpx2.Request("POST", "https://api.openai.com/v1/responses")
        ),
    ]
    error, _ = turn_fails(FakePlanClient(dropped))
    assert "Couldn't reach OpenAI" in str(error)


def test_a_sign_in_problem_stops_the_request_before_anything_is_sent():
    client = FakePlanClient()
    plan = FakePlan(
        error=SignInRequired("You're not signed in with ChatGPT. Run `wren login`.")
    )
    error, _ = turn_fails(client, plan)
    assert isinstance(error, SignInRequired)
    assert client.tokens == [] and client.requests == []


# --- Models, prices, and who pays ----------------------------------------------------


def test_models_come_from_the_accounts_own_list_in_its_order():
    listed = {
        "models": [
            {
                "slug": "gpt-6.1-sol",
                "display_name": "GPT-6.1 Sol",
                "visibility": "list",
            },
            {"slug": "internal-thing", "visibility": "hide"},
            {"slug": "gpt-6-luna", "display_name": "GPT-6 Luna", "visibility": "list"},
        ]
    }
    provider = plan_provider(FakePlanClient(models=listed))
    assert provider.models() == ["gpt-6.1-sol", "gpt-6-luna"]
    with pytest.raises(PlanRequestError, match="expected form"):
        plan_provider(FakePlanClient(models={"data": []})).models()


def test_plan_usage_has_no_price_and_says_who_pays():
    agent = Agent(plan_provider(FakePlanClient()), "sys", 100)
    assert agent.provider.prices() is None and agent.provider.billing == "ChatGPT plan"
    shown = run_command(agent, "/usage").text
    assert "Paid for by: your ChatGPT plan, signed in as ben@example.com" in shown
    assert "https://chatgpt.com/settings/usage" in shown


def test_the_api_key_route_is_unchanged():
    provider = OpenAIProvider("gpt-6.1-sol", client=object())
    assert provider.plan is None and provider.billing == "API key"
    assert provider.prices() is not None


# --- Choosing the route: once, explicitly ----------------------------------------------


def test_the_route_comes_from_the_flag_or_the_environment_and_defaults_to_the_key():
    assert Config.load(env={}).auth == API_KEY
    assert Config.load(env={"WREN_OPENAI_AUTH": "chatgpt"}).auth == CHATGPT
    assert (
        Config.load(auth="api-key", env={"WREN_OPENAI_AUTH": "chatgpt"}).auth == API_KEY
    )
    with pytest.raises(ValueError, match="WREN_OPENAI_AUTH"):
        Config.load(env={"WREN_OPENAI_AUTH": "plan"})


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty home folder, so the real ~/.wren/auth is never read."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_without_a_sign_in_the_plan_route_says_how_to_sign_in(
    home, monkeypatch, capsys
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")  # there, but not used
    with pytest.raises(SignInRequired, match="wren login"):
        make_provider("openai", "gpt-6-luna", auth=CHATGPT)
    assert cli.main(["--provider", "openai", "--auth", "chatgpt"]) == 1
    assert "wren login" in capsys.readouterr().err


def test_the_plan_route_doesnt_need_an_api_key(home, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    saved(CredentialStore(home / ".wren" / "auth"))
    provider = make_provider("openai", "gpt-6-luna", auth=CHATGPT)
    assert provider.billing == "ChatGPT plan" and provider.client is None


def test_switching_provider_keeps_the_route_it_started_with(home, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    agent = Agent(FakeProvider(["hi"]), "sys", 100, auth=CHATGPT)

    # Not signed in: the switch fails, and the API key isn't used instead.
    note, ok = switch_provider(agent, "openai")
    assert not ok and "wren login" in note and agent.provider.name == "fake"
    assert any("needs `wren login`" in label for label, _ in provider_choices(agent))

    saved(CredentialStore(home / ".wren" / "auth"))
    note, ok = switch_provider(agent, "openai")
    assert ok and "Paid for by your ChatGPT plan" in note
    assert agent.provider.billing == "ChatGPT plan"


def test_auth_status_from_the_command_line(home, capsys):
    saved(CredentialStore(home / ".wren" / "auth"))
    assert cli.main(["auth-status"]) == 0
    shown = capsys.readouterr().out
    assert "ben@example.com" in shown and CLIENT in shown and "at-1" not in shown
