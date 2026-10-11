from contextlib import closing

import pytest

from wren.agent import Agent
from wren.messages import Completed, TextDelta
from wren.providers.fake import FakeProvider


def run(agent, text):
    """Drive a full turn and return the events, like the REPL would."""
    return list(agent.run_turn(text))


def test_a_turn_streams_text_then_completes():
    agent = Agent(FakeProvider(["Hello there"]), "sys", 100)
    events = run(agent, "hi")

    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hello ", "there"]
    assert isinstance(events[-1], Completed)


def test_each_turn_appends_user_then_assistant():
    agent = Agent(FakeProvider(["one", "two"]), "sys", 100)
    run(agent, "first")
    run(agent, "second")

    assert [(m.role, m.text) for m in agent.messages] == [
        ("user", "first"),
        ("assistant", "one"),
        ("user", "second"),
        ("assistant", "two"),
    ]


def test_the_whole_history_is_sent_every_time():
    # The API is stateless: turn 2 must include turn 1, or the model forgets it.
    provider = FakeProvider(["one", "two"])
    agent = Agent(provider, "be brief", 100)
    run(agent, "first")
    run(agent, "second")

    second_call = provider.calls[1]
    assert [m.text for m in second_call["messages"]] == ["first", "one", "second"]
    assert second_call["system"] == "be brief"


def test_usage_adds_up_across_turns():
    agent = Agent(FakeProvider(["aaaa", "bbbbbbbb"]), "", 100)
    run(agent, "x")
    run(agent, "y")
    assert agent.usage.output_tokens == 1 + 2  # 4 chars -> 1 token, 8 chars -> 2


def test_abandoning_a_turn_rolls_the_conversation_back():
    # What happens when you press Ctrl-C mid-reply: the UI stops iterating.
    agent = Agent(FakeProvider(["kept", "a long reply that gets cut off"]), "", 100)
    run(agent, "first")

    with closing(agent.run_turn("second")) as events:
        next(events)  # read one fragment, then walk away

    assert [m.text for m in agent.messages] == ["first", "kept"]


def test_a_provider_error_rolls_the_conversation_back():
    agent = Agent(FakeProvider([]), "", 100)  # no replies: the provider raises
    with pytest.raises(RuntimeError):
        run(agent, "hello?")
    assert agent.messages == []


def test_reset_clears_the_conversation():
    agent = Agent(FakeProvider(["ok"]), "", 100)
    run(agent, "hi")
    agent.reset()
    assert agent.messages == []
