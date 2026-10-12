"""Tests for saved sessions (writing, reading back, resuming) and for
switching models."""

import json
import stat

import pytest

from wren.agent import NEVER_RECORDED, RESUMED_NOTE, Agent
from wren.commands import ago, effort_list, resume_session, run_command
from wren.messages import Message, ReasoningBlock, TextBlock, ToolResultBlock, ToolUseBlock, history_problems
from wren.prompts import COMPACTED_NOTE
from wren.providers.fake import FakeProvider
from wren.sessions import SessionError, SessionStore, load, message_from_json, message_to_json, project_folder_name
from wren.tools import Tool, ToolInput, ToolRegistry, ToolResult


class Shout(Tool):
    name = "shout"
    description = "Return the text in capitals."

    class Input(ToolInput):
        text: str

    def run(self, args, ctx):
        return ToolResult(args.text.upper(), summary="shouted")


def asks(*calls):
    return Message("assistant", [TextBlock("On it."), *(ToolUseBlock(i, n, a) for i, n, a in calls)])


def make(tmp_path, replies, project="project"):
    """An agent whose conversation is saved under tmp_path/home."""
    (tmp_path / project).mkdir(exist_ok=True)
    provider = FakeProvider(replies)
    agent = Agent(provider, "sys", 100, ToolRegistry([Shout()]))
    store = SessionStore(tmp_path / project, provider.name, provider.model, home=tmp_path / "home")
    agent.journal = store
    return agent, store


def run(agent, text):
    return list(agent.run_turn(text))


def lines(store):
    return [json.loads(line) for line in store.path.read_text().splitlines()]


# --- Writing ---------------------------------------------------------------------


def test_every_kind_of_block_survives_being_saved_and_read_back():
    message = Message(
        "assistant",
        [
            ReasoningBlock("anthropic", {"type": "thinking", "thinking": "", "signature": "s1"}),
            TextBlock("héllo ✓"),
            ToolUseBlock("t1", "read_file", {"path": "a.py", "limit": 5}),
        ],
    )
    result = Message("user", [ToolResultBlock("t1", "boom", is_error=True)])
    for original in (message, result):
        assert message_from_json(json.loads(json.dumps(message_to_json(original)))) == original


def test_nothing_is_written_until_something_is_said(tmp_path):
    agent, store = make(tmp_path, [])
    assert store.path is None and not (tmp_path / "home").exists()


def test_each_message_is_on_disk_as_soon_as_it_happens(tmp_path):
    agent, store = make(tmp_path, [asks(("t1", "shout", {"text": "hi"})), "Done."])
    events = agent.run_turn("go")
    next(events)  # one event in: the user's message is already saved
    assert [r["type"] for r in lines(store)] == ["meta", "message"]

    list(events)
    saved = lines(store)
    assert saved[0]["cwd"] == str((tmp_path / "project").resolve()) and saved[0]["model"] == "scripted"
    assert [r["role"] for r in saved if r["type"] == "message"] == ["user", "assistant", "user", "assistant"]
    assert [r["type"] for r in saved].count("spent") == 2  # one line per model call
    assert load(store.path).messages == agent.messages


def test_session_files_are_private_and_kept_per_project(tmp_path):
    agent, store = make(tmp_path, ["hello"])
    run(agent, "hi")
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert store.path.parent.name == project_folder_name((tmp_path / "project").resolve())
    assert store.path.parent.parent == tmp_path / "home" / ".wren" / "sessions"


def test_a_very_long_project_path_still_gets_a_short_unique_folder():
    from pathlib import Path

    a, b = Path("/x/" + "a" * 300 + "/one"), Path("/x/" + "a" * 300 + "/two")
    assert len(project_folder_name(a)) < 120 and project_folder_name(a) != project_folder_name(b)


def test_an_interrupted_turn_is_saved_with_its_loose_ends_tied(tmp_path):
    agent, store = make(tmp_path, [asks(("t1", "shout", {"text": "hi"}))])
    turn = agent.run_turn("go")
    for event in turn:
        if type(event).__name__ == "ToolStarted":
            break
    turn.close()  # the user stops it
    assert load(store.path).messages == agent.messages
    assert history_problems(load(store.path).messages) == []


def test_compaction_is_logged_as_a_replacement_not_a_rewrite(tmp_path):
    agent, store = make(tmp_path, ["a reply", "the summary", "after"])
    run(agent, "one")
    before = store.path.read_text()
    list(agent.compact())
    run(agent, "two")

    assert store.path.read_text().startswith(before)  # nothing already written was touched
    assert "replaced" in [r["type"] for r in lines(store)]
    assert [m.text for m in load(store.path).messages] == [COMPACTED_NOTE, "the summary", "two", "after"]


def test_clearing_starts_a_new_session_and_keeps_the_old_one(tmp_path):
    agent, store = make(tmp_path, ["first", "second"])
    run(agent, "one")
    old = store.path
    agent.reset()
    run(agent, "two")
    assert store.path != old and old.exists()
    assert [s.title for s in store.saved()] == ["two", "one"]  # most recent first


def test_a_disk_problem_does_not_stop_the_conversation(tmp_path):
    agent, store = make(tmp_path, ["hello"])
    (tmp_path / "home").write_text("a file where the folder should be")
    run(agent, "hi")
    assert agent.messages[-1].text == "hello"


# --- Reading back --------------------------------------------------------------------


def test_a_half_written_last_line_is_ignored_but_damage_elsewhere_is_not(tmp_path):
    agent, store = make(tmp_path, ["hello"])
    run(agent, "hi")
    good = store.path.read_text()

    store.path.write_text(good + '{"type": "message", "role": "user", "cont')  # killed mid-write
    assert len(load(store.path).messages) == 2

    store.path.write_text(good.replace('"type": "message", "role": "assistant"', '"type": "message", "roll"', 1) + good)
    with pytest.raises(SessionError, match="damaged at line"):
        load(store.path)


def test_find_takes_the_latest_or_an_id_prefix(tmp_path):
    agent, store = make(tmp_path, ["first", "second"])
    with pytest.raises(SessionError, match="no saved sessions"):
        store.find(None)
    run(agent, "one")
    first = store.current_id
    agent.reset()
    run(agent, "two")

    assert store.find(None).id == store.current_id
    assert store.find(first).id == first
    with pytest.raises(SessionError, match="no session here"):
        store.find("nope")


def test_sessions_from_other_folders_are_not_listed(tmp_path):
    agent, store = make(tmp_path, ["hello"], project="one")
    run(agent, "hi")
    _, other = make(tmp_path, [], project="two")
    assert other.saved() == []


# --- Resuming ---------------------------------------------------------------------------


def test_resuming_brings_the_conversation_back_and_carries_on_in_the_same_file(tmp_path):
    agent, store = make(tmp_path, [asks(("t1", "shout", {"text": "hi"})), "Done."])
    run(agent, "go")
    path = store.path

    later, later_store = make(tmp_path, ["and more"])  # a new run of Wren, later
    note = resume_session(later, later_store, later_store.find(None))
    assert "Resumed" in note and "4 messages" in note
    assert later.messages == agent.messages and later.context_tokens > 0

    run(later, "more?")
    assert later_store.path == path and len(load(path).messages) == 6
    assert history_problems(later.messages) == []


def test_resuming_restores_the_token_totals_and_the_size_exactly(tmp_path):
    agent, store = make(tmp_path, [asks(("t1", "shout", {"text": "hi"})), "Done.", "the summary", "after"])
    run(agent, "go")
    list(agent.compact())
    run(agent, "more")
    assert agent.usage.sent_tokens > 0 and agent.context_tokens > 0

    later, later_store = make(tmp_path, [])
    resume_session(later, later_store, later_store.find(None))
    assert later.usage == agent.usage  # every model call, the compaction's included
    assert later.context_tokens == agent.context_tokens  # the measured size, not a guess
    assert "0 tokens in" not in run_command(later, "/usage").text


def test_a_session_file_with_no_usage_lines_still_resumes_with_an_estimate(tmp_path):
    agent, store = make(tmp_path, ["a reply of some length"])
    run(agent, "hello there")
    kept = [line for line in store.path.read_text().splitlines() if '"type": "spent"' not in line]
    store.path.write_text("\n".join(kept) + "\n")  # as an older Wren would have written it

    later, later_store = make(tmp_path, [])
    resume_session(later, later_store, later_store.find(None))
    assert later.usage.sent_tokens == 0 and later.context_tokens > 0


def test_clearing_resets_the_totals_for_the_new_conversation(tmp_path):
    agent, store = make(tmp_path, ["hello"])
    run(agent, "hi")
    agent.reset()
    assert agent.usage.sent_tokens == 0 and agent.context_tokens == 0


def test_a_small_conversation_shows_as_under_one_percent_not_zero(tmp_path):
    agent, store = make(tmp_path, ["hello"])
    agent.context_budget = 150_000
    assert "0 tokens, 0% of" in run_command(agent, "/usage").text  # nothing yet: 0% is true
    run(agent, "hi")
    assert "<1% of the 150,000 budget" in run_command(agent, "/usage").text


def test_resuming_a_session_that_was_killed_mid_turn_ties_it_off(tmp_path):
    agent, store = make(tmp_path, [asks(("t1", "shout", {"text": "hi"}))])
    # Killed outright while the tool was pending: no chance to tidy up.
    store.append(Message.user("go"))
    store.append(asks(("t1", "shout", {"text": "hi"})))

    later, later_store = make(tmp_path, ["carrying on"])
    resume_session(later, later_store, later_store.find(None))
    assert later.messages[2].tool_results[0].content == NEVER_RECORDED
    assert later.messages[3].text == RESUMED_NOTE
    assert history_problems(later.messages) == []
    assert load(later_store.path).messages == later.messages  # the tidy-up was saved too

    run(later, "continue")
    assert history_problems(later.messages) == []


def test_resuming_restores_the_model_the_session_was_using(tmp_path):
    agent, store = make(tmp_path, ["hello"])
    agent.set_model("demo-large")
    run(agent, "hi")

    later, later_store = make(tmp_path, [])
    note = resume_session(later, later_store, later_store.find(None))
    assert later.provider.model == "demo-large" and "Using demo-large, as it was" in note


def test_a_session_that_cannot_be_made_valid_is_refused_and_nothing_changes(tmp_path):
    agent, store = make(tmp_path, ["hello"])
    run(agent, "hi")
    current = list(agent.messages)
    with pytest.raises(ValueError, match="not usable"):
        agent.restore([Message.assistant("an answer to nothing")])
    assert agent.messages == current


def test_the_resume_command(tmp_path):
    agent, store = make(tmp_path, ["first", "second"])
    run(agent, "one")
    first = store.current_id
    agent.reset()
    run(agent, "two")

    assert run_command(agent, "/resume", sessions=store).pick_session
    assert "couldn't resume" in run_command(agent, "/resume zzz", sessions=store).text
    assert "the conversation you're in" in run_command(agent, f"/resume {store.current_id}", sessions=store).text

    result = run_command(agent, f"/resume {first}", sessions=store)
    assert result.resumed and [m.text for m in agent.messages] == ["one", "first"]


# --- Switching models ----------------------------------------------------------------------


def test_switching_model_takes_effect_on_the_next_request_and_is_recorded(tmp_path):
    agent, store = make(tmp_path, ["one", "two"])
    run(agent, "hi")
    assert run_command(agent, "/model").pick_model

    note = run_command(agent, "/model demo-large").text
    assert "Now using demo-large" in note and "full price" in note
    run(agent, "again")
    assert agent.provider.model == "demo-large"
    assert {"type": "model", "provider": "fake", "model": "demo-large", "effort": None} in lines(store)
    assert load(store.path).model == "demo-large"
    assert "Already using demo-large" in run_command(agent, "/model demo-large").text


# --- Effort ---------------------------------------------------------------------------------


def test_effort_is_sent_from_the_next_request_on_and_default_means_unset(tmp_path):
    agent, store = make(tmp_path, ["one", "two", "three"])
    run(agent, "hi")
    assert run_command(agent, "/effort").pick_effort

    note = run_command(agent, "/effort HIGH").text
    assert "Effort is now high." in note and "resets the cache" in note
    run(agent, "again")
    assert [call["effort"] for call in agent.provider.calls] == [None, "high"]
    assert "Effort asked for: high" in run_command(agent, "/usage").text
    assert "already high" in run_command(agent, "/effort high").text

    assert "Effort is now the model's default." in run_command(agent, "/effort default").text
    run(agent, "and again")
    assert agent.provider.calls[-1]["effort"] is None


def test_switching_model_puts_effort_back_to_that_models_default(tmp_path):
    agent, store = make(tmp_path, [])
    agent.set_effort("high")
    note = run_command(agent, "/model demo-large").text
    assert agent.provider.effort is None and "Effort is back to this model's default" in note


def test_a_model_with_no_separate_effort_setting_points_you_to_the_model_list(tmp_path):
    # The other kind of provider: effort, if there's a choice, is part of the model's name.
    agent, store = make(tmp_path, [])
    assert effort_list(agent) == (["low", "medium", "high"], "")
    agent.set_model("demo-fast")
    levels, note = effort_list(agent)
    assert levels == [] and "no separate effort setting" in note and "/model" in note


def test_a_failure_to_list_effort_levels_is_reported_not_raised(tmp_path):
    agent, store = make(tmp_path, [])
    agent.provider.efforts = lambda: 1 / 0
    levels, note = effort_list(agent)
    assert levels == [] and "couldn't find out" in note and "/effort LEVEL" in note


def test_a_session_remembers_its_effort_and_resuming_restores_it(tmp_path):
    agent, store = make(tmp_path, ["hello"])
    agent.set_model("demo-large")
    agent.set_effort("low")
    run(agent, "hi")
    assert lines(store)[0]["effort"] == "low"  # set before the file existed, so the first line carries it

    later, later_store = make(tmp_path, [])
    note = resume_session(later, later_store, later_store.find(None))
    assert (later.provider.model, later.provider.effort) == ("demo-large", "low")
    assert "Using demo-large at low effort, as it was" in note


def test_usage_says_what_the_default_effort_is_and_where_that_comes_from(tmp_path):
    agent, store = make(tmp_path, [])
    shown = run_command(agent, "/usage").text
    assert "Effort asked for: the model's default, which is medium for scripted" in shown
    assert "(the fake provider's own rule)" in shown  # the source is named: it isn't from the API

    agent.set_model("demo-fast")  # a model whose default isn't known
    assert "Wren doesn't know what that is for demo-fast" in run_command(agent, "/usage").text


def test_usage_separates_what_wren_sent_from_what_the_provider_reported(tmp_path):
    agent, store = make(tmp_path, ["one", "two"])
    assert "Last request: (none yet)" in run_command(agent, "/usage").text

    run(agent, "hi")
    first = run_command(agent, "/usage").text
    assert "Last request was sent with: no effort setting, so the model's default applied" in first
    assert "(Wren's record: fake doesn't report the effort it used)" in first
    assert "Thinking in the last reply: 0 of" in first and "(as reported by fake" in first

    agent.set_effort("high")
    assert "sent with: no effort setting" in run_command(agent, "/usage").text  # asked for, not yet sent
    run(agent, "again")
    second = run_command(agent, "/usage").text
    assert "Last request was sent with: effort high" in second
    assert "Thinking in the last reply: 200 of" in second  # the measurable effect


def test_thinking_tokens_are_saved_and_restored_with_the_other_totals(tmp_path):
    agent, store = make(tmp_path, ["one"])
    agent.set_effort("high")
    run(agent, "hi")
    assert load(store.path).usage.thinking_tokens == 200


def test_usage_shows_which_model_actually_answered(tmp_path):
    agent, store = make(tmp_path, ["one", "two"])
    assert "Last reply came from: (no reply yet)" in run_command(agent, "/usage").text
    run(agent, "hi")
    assert "Last reply came from: scripted  (as reported by fake)" in run_command(agent, "/usage").text

    agent.set_model("demo-large")
    asked_not_yet_answered = run_command(agent, "/usage").text
    assert "Model asked for: demo-large" in asked_not_yet_answered and "came from: scripted" in asked_not_yet_answered
    run(agent, "again")
    assert "Last reply came from: demo-large" in run_command(agent, "/usage").text


def test_ago():
    from datetime import datetime, timedelta

    now = datetime(2026, 10, 12, 12, 0)
    assert ago(now - timedelta(seconds=30), now) == "just now"
    assert ago(now - timedelta(minutes=1), now) == "1 minute ago"
    assert ago(now - timedelta(hours=5), now) == "5 hours ago"
    assert ago(now - timedelta(days=3), now) == "3 days ago"
