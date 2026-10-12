from contextlib import closing

import pytest

from wren.agent import (
    CUT_SHORT,
    INTERRUPTED_NOTE,
    NOT_RUN,
    Agent,
    Compacted,
    CompactionStarted,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from wren.messages import (
    Completed,
    Message,
    ReasoningBlock,
    TextBlock,
    TextDelta,
    ToolUseBlock,
    Usage,
    history_problems,
)
from wren.prompts import COMPACT_PROMPT, COMPACTED_NOTE
from wren.providers.fake import FakeProvider
from wren.tools import Decision, Permissions, Tool, ToolInput, ToolRegistry, ToolResult, default_tools
from wren.tools.registry import DECLINED

# --- Test doubles ---------------------------------------------------------


class Shout(Tool):
    name = "shout"
    description = "Return the text in capitals."

    class Input(ToolInput):
        text: str

    def run(self, args, ctx):
        return ToolResult(args.text.upper(), summary="shouted")


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


# --- Turns that are cut short -------------------------------------------------
#
# Tools can now change things, so a turn that stops early is kept, not rolled
# back: the model must remember what it really did. Each test checks both that
# the history says what happened and that it's still valid.


def test_an_interrupted_reply_keeps_what_was_said_and_notes_the_interruption():
    # What happens when you press Ctrl-C mid-reply: the UI stops iterating.
    agent, _ = make_agent(["kept", "a long reply that gets cut off"])
    run(agent, "first")

    with closing(agent.run_turn("second")) as events:
        next(events)  # read one fragment, then walk away

    assert [m.text for m in agent.messages[:3]] == ["first", "kept", "second"]
    assert agent.messages[3].text == f"a\n\n{INTERRUPTED_NOTE}"
    assert history_problems(agent.messages) == []


def test_interrupting_between_tools_keeps_finished_results_and_answers_the_rest():
    agent, _ = make_agent([asks(("t1", "shout", {"text": "one"}), ("t2", "shout", {"text": "two"}))])
    with closing(agent.run_turn("go")) as events:
        for event in events:
            if isinstance(event, ToolFinished):
                break  # t1 has run; t2 never starts

    first, second = agent.messages[2].tool_results
    assert (first.tool_use_id, first.content, first.is_error) == ("t1", "ONE", False)
    assert (second.tool_use_id, second.content, second.is_error) == ("t2", NOT_RUN, True)
    assert agent.messages[3].text == INTERRUPTED_NOTE
    assert history_problems(agent.messages) == []


def test_interrupting_a_running_tool_says_it_may_have_partly_completed():
    class Stuck(Tool):
        name = "stuck"
        description = "Never finishes."

        def run(self, args, ctx):
            raise KeyboardInterrupt  # Ctrl-C arrives while the tool is working

    agent = Agent(FakeProvider([asks(("t1", "stuck", {}))]), "sys", 100, ToolRegistry([Stuck()]))
    with pytest.raises(KeyboardInterrupt):
        run(agent, "go")

    assert agent.messages[2].tool_results[0].content == CUT_SHORT
    assert history_problems(agent.messages) == []


def test_a_provider_error_is_noted_and_the_conversation_carries_on():
    agent, provider = make_agent([asks(("t1", "shout", {"text": "hi"}))])  # then the script runs out
    with pytest.raises(RuntimeError):
        run(agent, "go")

    assert agent.messages[2].tool_results[0].content == "HI"  # the tool call that did happen is kept
    assert "because of an error" in agent.messages[3].text and "ran out of scripted replies" in agent.messages[3].text
    assert history_problems(agent.messages) == []

    provider.replies.append("back again")
    run(agent, "still there?")
    assert history_problems(agent.messages) == []


# --- Permissions, end to end ----------------------------------------------------


def writing_agent(tmp_path, replies, ask):
    tools = ToolRegistry(default_tools(), root=tmp_path, permissions=Permissions(ask))
    return Agent(FakeProvider(replies), "sys", 100, tools)


def test_an_approved_write_happens_and_a_declined_one_does_not(tmp_path):
    answers = iter([Decision.ALLOW, Decision.DENY])
    agent = writing_agent(
        tmp_path,
        [
            asks(("t1", "write_file", {"path": "yes.txt", "content": "ok\n"})),
            asks(("t2", "write_file", {"path": "no.txt", "content": "ok\n"})),
            "I'll leave it there.",
        ],
        ask=lambda request: next(answers),
    )
    run(agent, "write two files")

    assert (tmp_path / "yes.txt").read_text() == "ok\n"
    assert not (tmp_path / "no.txt").exists()
    declined = agent.messages[4].tool_results[0]
    assert declined.is_error and declined.content == DECLINED
    assert history_problems(agent.messages) == []


def test_ctrl_c_at_a_permission_prompt_stops_the_turn_and_changes_nothing(tmp_path):
    def ask(request):
        raise KeyboardInterrupt

    agent = writing_agent(tmp_path, [asks(("t1", "write_file", {"path": "a.txt", "content": "x"}))], ask)
    with pytest.raises(KeyboardInterrupt):
        run(agent, "go")

    assert not (tmp_path / "a.txt").exists()
    assert history_problems(agent.messages) == []


# --- Reasoning blocks: kept, and handed back untouched -----------------------------


def test_the_models_reasoning_is_kept_in_place_and_sent_back_unchanged():
    sealed = ReasoningBlock("fake", {"type": "thinking", "thinking": "", "signature": "abc123"})
    request = Message("assistant", [sealed, TextBlock("Let me check."), ToolUseBlock("t1", "shout", {"text": "hi"})])
    agent, provider = make_agent([request, "Done."])
    run(agent, "go")

    resent = provider.calls[1]["messages"][1]  # the assistant message, as sent with the tool result
    assert resent.content[0] is sealed and resent.content[0].data["signature"] == "abc123"
    assert history_problems(agent.messages) == []


def test_a_reply_that_is_only_reasoning_gets_a_placeholder_after_it():
    sealed = ReasoningBlock("fake", {"type": "thinking", "thinking": "", "signature": "s"})
    agent, _ = make_agent([Message("assistant", [sealed])])
    run(agent, "hi")
    assert agent.messages[1].content == [sealed, TextBlock("(no reply)")]


# --- Context size and compaction ------------------------------------------------------


def test_the_agent_tracks_how_big_the_conversation_is():
    agent, provider = make_agent(["one"])
    assert agent.context_tokens == 0
    events = run(agent, "hi")
    used = [e for e in events if isinstance(e, Completed)][0].usage
    assert agent.context_tokens == used.sent_tokens + used.output_tokens > 0


def test_compact_replaces_the_history_with_the_models_summary():
    agent, provider = make_agent(["first reply", "second reply", "## What the user wants\nA summary."])
    run(agent, "one")
    run(agent, "two")
    events = list(agent.compact())

    assert isinstance(events[0], CompactionStarted) and not events[0].automatic
    assert isinstance(events[-1], Compacted) and not events[-1].error
    assert events[-1].after_tokens < events[-1].before_tokens
    assert not any(isinstance(e, TextDelta) for e in events)  # a summary isn't streamed as a reply
    assert [m.text for m in agent.messages] == [COMPACTED_NOTE, "## What the user wants\nA summary."]
    assert history_problems(agent.messages) == []


def test_the_summary_request_reuses_the_same_prefix_so_it_can_be_cached():
    agent, provider = make_agent(["a reply", "summary"])
    run(agent, "one")
    list(agent.compact())

    normal, summarising = provider.calls
    assert summarising["system"] == normal["system"] and summarising["tools"] == normal["tools"]
    assert [m.text for m in summarising["messages"]] == ["one", "a reply", COMPACT_PROMPT]


def test_compaction_happens_by_itself_between_turns_once_over_budget():
    agent, provider = make_agent(["a reply", "the summary", "an answer"])
    agent.context_budget = 10  # tiny, so one turn is enough to pass it
    run(agent, "one")
    events = run(agent, "two")

    assert isinstance(events[0], CompactionStarted) and events[0].automatic
    assert isinstance(events[1], Compacted)
    assert [m.text for m in agent.messages] == [COMPACTED_NOTE, "the summary", "two", "an answer"]
    assert history_problems(agent.messages) == []


def test_no_budget_means_no_automatic_compaction():
    agent, provider = make_agent(["a", "b"])  # context_budget defaults to 0
    run(agent, "one")
    assert not any(isinstance(e, CompactionStarted) for e in run(agent, "two"))


@pytest.mark.parametrize(
    "bad_summary, problem",
    [
        (asks(("t1", "shout", {"text": "x"}), text="I'll look first."), "didn't write a summary"),
        (Message("assistant", [TextBlock("   ")]), "didn't write a summary"),
        (None, "ran out of scripted replies"),  # the provider itself fails
    ],
)
def test_a_failed_compaction_leaves_the_history_exactly_as_it_was(bad_summary, problem):
    agent, provider = make_agent(["a reply"] + ([bad_summary] if bad_summary else []))
    run(agent, "one")
    before = list(agent.messages)
    result = list(agent.compact())[-1]

    assert problem in result.error and result.after_tokens == result.before_tokens
    assert agent.messages == before


def test_a_failed_automatic_compaction_does_not_stop_the_turn():
    agent, provider = make_agent(["a reply", Message("assistant", [TextBlock(" ")]), "an answer"])
    agent.context_budget = 10
    run(agent, "one")
    events = run(agent, "two")
    assert events[1].error and isinstance(events[-1], TurnFinished)
    assert [m.text for m in agent.messages] == ["one", "a reply", "two", "an answer"]


def test_abandoning_a_compaction_changes_nothing():
    agent, provider = make_agent(["a reply", "a summary"])
    run(agent, "one")
    with closing(agent.compact()) as events:
        next(events)  # CompactionStarted, then walk away
    assert [m.text for m in agent.messages] == ["one", "a reply"]


def test_there_must_be_something_to_compact():
    agent, _ = make_agent([])
    assert "nothing to compact" in list(agent.compact())[0].error


def test_compaction_drops_reasoning_and_counts_its_own_cost():
    sealed = ReasoningBlock("fake", {"type": "thinking", "thinking": "", "signature": "s"})
    agent, _ = make_agent(["a reply", Message("assistant", [sealed, TextBlock("The summary.")])])
    run(agent, "one")
    spent = agent.usage.output_tokens
    list(agent.compact())
    assert agent.messages[1].content == [TextBlock("The summary.")]
    assert agent.usage.output_tokens > spent


def test_usage_adds_up_cached_tokens_too():
    total = Usage(10, 2, cache_read_tokens=100, cache_write_tokens=5) + Usage(1, 1, 50, 0)
    assert (total.input_tokens, total.cache_read_tokens, total.cache_write_tokens) == (11, 150, 5)
    assert total.sent_tokens == 166


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
