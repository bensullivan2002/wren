from contextlib import closing

import pytest

from wren.agent import Agent, ToolFinished, ToolStarted, TurnFinished
from wren.messages import Completed, Message, TextBlock, TextDelta, ToolUseBlock, history_problems
from wren.providers.fake import FakeProvider
from wren.tools import Tool, ToolRegistry, ToolResult

# --- Test doubles ---------------------------------------------------------


class Shout(Tool):
    name = "shout"
    description = "Return the text in capitals."
    input_schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    def run(self, args, ctx):
        return ToolResult(args["text"].upper(), summary="shouted")


def make_agent(replies, max_steps=10):
    provider = FakeProvider(replies)
    agent = Agent(provider, "sys", 100, ToolRegistry([Shout()]), max_steps=max_steps)
    return agent, provider


def asks(*calls, text="Let me check."):
    """An assistant reply that asks for tools: asks(("t1", "shout", {"text": "hi"}), ...)"""
    return Message("assistant", [TextBlock(text), *(ToolUseBlock(i, n, a) for i, n, a in calls)])


def run(agent, text):
    """Drive a full turn and return the events, like the REPL would."""
    return list(agent.run_turn(text))


# --- Plain chat (no tools) ------------------------------------------------


def test_a_plain_turn_streams_text_then_finishes():
    agent, _ = make_agent(["Hello there"])
    events = run(agent, "hi")

    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hello ", "there"]
    assert isinstance(events[-2], Completed)
    assert events[-1] == TurnFinished(events[-2].usage, steps=1)


def test_each_plain_turn_appends_user_then_assistant():
    agent, _ = make_agent(["one", "two"])
    run(agent, "first")
    run(agent, "second")

    assert [(m.role, m.text) for m in agent.messages] == [
        ("user", "first"),
        ("assistant", "one"),
        ("user", "second"),
        ("assistant", "two"),
    ]
    assert history_problems(agent.messages) == []


def test_the_whole_history_and_the_tools_are_sent_every_time():
    agent, provider = make_agent(["one", "two"])
    run(agent, "first")
    run(agent, "second")

    second_call = provider.calls[1]
    assert [m.text for m in second_call["messages"]] == ["first", "one", "second"]
    assert second_call["system"] == "sys"
    assert [t.name for t in second_call["tools"]] == ["shout"]


# --- The agent loop -------------------------------------------------------


def test_a_tool_request_is_run_and_the_result_sent_back():
    agent, provider = make_agent([asks(("t1", "shout", {"text": "hi"})), "It says HI."])
    events = run(agent, "shout hi")

    kinds = [type(e).__name__ for e in events if not isinstance(e, TextDelta)]
    assert kinds == ["Completed", "ToolStarted", "ToolFinished", "Completed", "TurnFinished"]
    assert events[-1].steps == 2

    # The second model call sees the tool result, in a user message.
    sent = provider.calls[1]["messages"]
    assert sent[-1].role == "user"
    assert sent[-1].tool_results[0].tool_use_id == "t1"
    assert sent[-1].tool_results[0].content == "HI"

    assert [m.role for m in agent.messages] == ["user", "assistant", "user", "assistant"]
    assert history_problems(agent.messages) == []


def test_several_tool_requests_in_one_reply_are_answered_together_in_order():
    agent, _ = make_agent([asks(("a", "shout", {"text": "x"}), ("b", "shout", {"text": "y"})), "done"])
    run(agent, "go")

    results = agent.messages[2].tool_results
    assert [(r.tool_use_id, r.content) for r in results] == [("a", "X"), ("b", "Y")]
    assert history_problems(agent.messages) == []


def test_a_failing_tool_becomes_an_error_result_and_the_loop_goes_on():
    agent, _ = make_agent([asks(("t1", "no_such_tool", {})), "Sorry, that tool doesn't exist."])
    events = run(agent, "go")

    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.result.is_error
    assert agent.messages[2].tool_results[0].is_error
    assert agent.messages[-1].text == "Sorry, that tool doesn't exist."


def test_the_step_limit_stops_a_runaway_loop_and_leaves_valid_history():
    endless = [asks((f"t{i}", "shout", {"text": "again"})) for i in range(10)]
    agent, provider = make_agent(endless, max_steps=3)
    events = run(agent, "go")

    assert len(provider.calls) == 3
    assert events[-1].hit_step_limit
    assert "limit" in agent.messages[-1].text
    assert history_problems(agent.messages) == []


def test_an_empty_reply_gets_a_placeholder_so_the_history_stays_valid():
    agent, _ = make_agent([Message("assistant", [])])
    run(agent, "hi")
    assert agent.messages[-1].text == "(no reply)"
    assert history_problems(agent.messages) == []


# --- Usage ------------------------------------------------------------------


def test_usage_adds_up_across_calls_and_turns():
    agent, _ = make_agent(["aaaa", "bbbbbbbb"])
    run(agent, "x")
    run(agent, "y")
    assert agent.usage.output_tokens == 1 + 2  # 4 chars -> 1 token, 8 chars -> 2


def test_turn_usage_covers_every_model_call_in_the_turn():
    agent, _ = make_agent([asks(("t1", "shout", {"text": "hi"})), "done"])
    events = run(agent, "go")
    per_call = [e.usage for e in events if isinstance(e, Completed)]
    assert events[-1].usage.output_tokens == sum(u.output_tokens for u in per_call)


# --- Rolling back -----------------------------------------------------------


def test_abandoning_a_turn_rolls_the_conversation_back():
    # What happens when you press Ctrl-C mid-reply: the UI stops iterating.
    agent, _ = make_agent(["kept", "a long reply that gets cut off"])
    run(agent, "first")

    with closing(agent.run_turn("second")) as events:
        next(events)  # read one fragment, then walk away

    assert [m.text for m in agent.messages] == ["first", "kept"]


def test_abandoning_a_turn_mid_loop_rolls_back_the_tool_calls_too():
    agent, _ = make_agent([asks(("t1", "shout", {"text": "hi"})), "never reached"])
    with closing(agent.run_turn("go")) as events:
        for event in events:
            if isinstance(event, ToolStarted):
                break
    assert agent.messages == []


def test_a_provider_error_rolls_the_conversation_back():
    agent, _ = make_agent([])  # no replies: the provider raises
    with pytest.raises(RuntimeError):
        run(agent, "hello?")
    assert agent.messages == []


def test_reset_clears_the_conversation():
    agent, _ = make_agent(["ok"])
    run(agent, "hi")
    agent.reset()
    assert agent.messages == []


# --- The history checker itself ---------------------------------------------


def test_history_problems_spots_an_unanswered_tool_request():
    history = [Message.user("go"), asks(("t1", "shout", {"text": "x"}))]
    assert any("asks for tools" in p for p in history_problems(history))


def test_history_problems_spots_a_history_ending_mid_turn():
    history = [Message.user("go")]
    assert any("end with an assistant" in p for p in history_problems(history))
