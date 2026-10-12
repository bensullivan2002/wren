"""Tests for the full-screen app.

Textual can run an app "headless": no real terminal, but the same event
loop, widgets and key handling. `app.run_test()` gives a `pilot` that
presses keys for us. So these tests type, press Enter, answer questions and
press Escape, the way a person would, and then look at what's on screen.

The tests are `async def` because the app's event loop is asyncio's;
pytest-asyncio runs them (see asyncio_mode in pyproject.toml).
"""

import os

import pytest
from textual.widgets import Markdown, Static

from wren.agent import INTERRUPTED_NOTE, Agent
from wren.memory import Memory
from wren.messages import Message, TextBlock, ToolUseBlock, history_problems
from wren.providers.fake import FakeProvider
from wren.sessions import SessionStore
from wren.tools import Permissions, ToolRegistry, default_tools
from wren.tools.permissions import FileChange
from wren.tui import PermissionPrompt, Picker, WrenApp, change_text


def make_app(tmp_path, replies=None, memory=None, saved=False, choose_session=False):
    """An app on a throwaway project. With no `replies`, the fake model is in
    demo mode, where typing `read x`, `write x text` or `run cmd` asks for a tool.
    With `saved`, its conversations are written under tmp_path/home."""
    tools = ToolRegistry(default_tools(), root=tmp_path, permissions=Permissions())
    agent = Agent(FakeProvider(replies), "sys", 100, tools)
    sessions = None
    if saved:
        sessions = SessionStore(tmp_path, agent.provider.name, agent.provider.model, home=tmp_path / "home")
        agent.journal = sessions
    return WrenApp(agent, memory, sessions, choose_session=choose_session)


def asks(*calls):
    return Message("assistant", [TextBlock("On it."), *(ToolUseBlock(i, n, a) for i, n, a in calls)])


async def say(app, pilot, text):
    """Type a message and press Enter."""
    app.prompt.insert(text)
    await pilot.press("enter")


async def until(pilot, condition, what="the condition"):
    """Let the app run until `condition()` is true. The turn runs on another
    thread, so tests wait for its effects; they can't assume a fixed delay."""
    for _ in range(200):
        if condition():
            return
        await pilot.pause(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def finished(app, pilot):
    await until(pilot, lambda: not app.busy and app.agent.messages, "the turn to finish")
    await pilot.pause()


def screen_text(app):
    """Everything in the conversation, as plain text."""
    parts = []
    for widget in app.transcript.query("*"):
        if isinstance(widget, Markdown):
            parts.append(widget.source)
        elif isinstance(widget, Static):
            parts.append(str(widget.content))
    return "\n".join(parts)


def question(app):
    found = app.query(PermissionPrompt)
    return found.first() if found else None


# --- Talking ------------------------------------------------------------------------


async def test_a_message_gets_a_streamed_markdown_reply(tmp_path):
    app = make_app(tmp_path, ["Hello **there**"])
    async with app.run_test() as pilot:
        await say(app, pilot, "hi")
        await finished(app, pilot)

        assert "hi" in screen_text(app)
        assert app.query_one(Markdown).source == "Hello **there**"
        assert app.prompt.text == "" and app.focused is app.prompt  # ready for the next message
    assert history_problems(app.agent.messages) == []


async def test_text_that_looks_like_markup_is_shown_as_it_is(tmp_path):
    # "[bold]" means something to Textual. From a user or a tool, it's just text.
    (tmp_path / "odd.txt").write_text("[/]\n")
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "what does [bold red]this[/] do [/nonsense]")
        await finished(app, pilot)
        await say(app, pilot, "read [missing/].txt")
        await finished(app, pilot)
        assert "[bold red]this[/] do [/nonsense]" in screen_text(app)


async def test_a_second_message_is_refused_while_wren_is_working(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "write a.txt one")
        await until(pilot, lambda: question(app), "the permission question")
        app.prompt.insert("hello?")
        await pilot.press("enter")  # the question has the focus, so this does nothing harmful
        app.prompt.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.text == "hello?"  # kept, not sent
        assert len(app.agent.messages) == 2  # still the first turn: the user message and the tool request
        await pilot.press("escape")
        await finished(app, pilot)


# --- Tools and permission --------------------------------------------------------------


async def test_a_read_only_tool_runs_without_asking_and_shows_a_summary(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "read a.txt")
        await finished(app, pilot)
        assert "read_file(path='a.txt')  → 2 lines" in screen_text(app)


async def test_an_edit_asks_first_and_y_lets_it_happen(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "edit a.txt one => 1")
        await until(pilot, lambda: question(app), "the permission question")

        assert app.focused is question(app)  # so that "y" is an answer, not a letter in the prompt
        shown = screen_text(app)
        assert "Edit a.txt" in shown and "-one" in shown and "+1" in shown
        assert "always allow all file edits this session" in shown
        assert (tmp_path / "a.txt").read_text() == "one\ntwo\n"  # nothing has happened yet

        await pilot.press("y")
        await finished(app, pilot)
        assert (tmp_path / "a.txt").read_text() == "1\ntwo\n"
        assert question(app) is None and app.focused is app.prompt
        after = screen_text(app)
        assert "→ a.txt +1 −1" in after and "-one" in after  # the diff stays in the conversation


async def test_n_declines_and_the_model_is_told(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "write new.txt hello")
        await until(pilot, lambda: question(app), "the permission question")
        await pilot.press("n")
        await finished(app, pilot)

        assert not (tmp_path / "new.txt").exists()
        assert "✗ declined" in screen_text(app)
    assert history_problems(app.agent.messages) == []


async def test_a_stops_the_asking_for_the_rest_of_the_session(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "write a.txt one")
        await until(pilot, lambda: question(app), "the permission question")
        await pilot.press("a")
        await finished(app, pilot)

        await say(app, pilot, "write b.txt two")
        await until(pilot, lambda: (tmp_path / "b.txt").exists(), "the second write, with no question")
        await finished(app, pilot)
        assert question(app) is None


@pytest.mark.skipif(os.name != "posix", reason="uses POSIX shell commands")
async def test_a_command_shows_the_end_of_its_output_and_always_is_not_offered_for_shell_syntax(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "run echo one; echo two")
        await until(pilot, lambda: question(app), "the permission question")
        assert "always allow" not in screen_text(app)
        await pilot.press("a")  # not on offer: must not count as a yes
        await pilot.pause(0.1)
        assert question(app) is not None
        await pilot.press("y")
        await finished(app, pilot)
        assert "→ 2 lines\none\ntwo" in screen_text(app)


async def test_a_question_in_a_long_conversation_appears_on_screen_not_above_it(tmp_path):
    # The bug: giving the question the keyboard scrolled the conversation to
    # its very top, leaving the question waiting off-screen at the bottom.
    app = make_app(tmp_path)

    def at_the_end():
        return app.transcript.max_scroll_y > 0 and app.transcript.scroll_y == app.transcript.max_scroll_y

    async with app.run_test(size=(100, 30)) as pilot:
        for i in range(10):
            await say(app, pilot, f"message {i} " + "word " * 30)
            await finished(app, pilot)
        await until(pilot, at_the_end, "the conversation to be scrolled to its end")

        await say(app, pilot, "write notes.txt hello")
        await until(pilot, lambda: question(app), "the permission question")
        await until(pilot, at_the_end, "the question to be in view")

        await pilot.press("y")
        await finished(app, pilot)
        await until(pilot, at_the_end, "the result to be in view")

        await say(app, pilot, "/model")
        await until(pilot, lambda: app.query(Picker), "the model picker")
        await until(pilot, at_the_end, "the picker to be in view")


async def test_sending_a_message_returns_to_the_end_after_scrolling_up(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        for i in range(10):
            await say(app, pilot, f"message {i} " + "word " * 30)
            await finished(app, pilot)
        app.transcript.scroll_home(animate=False)  # the user scrolls up to read something
        await pilot.pause()
        assert app.transcript.scroll_y == 0

        await say(app, pilot, "one more")
        await finished(app, pilot)
        await until(pilot, lambda: app.transcript.scroll_y == app.transcript.max_scroll_y, "a return to the end")


# --- Stopping -----------------------------------------------------------------------------


async def test_escape_at_a_question_stops_the_turn_and_changes_nothing(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "write new.txt hello")
        await until(pilot, lambda: question(app), "the permission question")
        await pilot.press("escape")
        await finished(app, pilot)

        assert not (tmp_path / "new.txt").exists()
        assert question(app) is None and app.focused is app.prompt
        assert "[stopped:" in screen_text(app)
    assert app.agent.messages[-1].text == INTERRUPTED_NOTE
    assert history_problems(app.agent.messages) == []


@pytest.mark.skipif(os.name != "posix", reason="uses POSIX shell commands")
async def test_escape_stops_a_running_command_promptly(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "run sleep 30")
        await until(pilot, lambda: question(app), "the permission question")
        await pilot.press("y")
        await pilot.pause(0.3)  # the command is now running
        await pilot.press("escape")
        await finished(app, pilot)  # would time out if we waited for sleep
    assert "Stopped by the user" in app.agent.messages[2].tool_results[0].content
    assert history_problems(app.agent.messages) == []


async def test_wren_can_be_used_again_after_a_stop(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "write new.txt hello")
        await until(pilot, lambda: question(app), "the permission question")
        await pilot.press("escape")
        await finished(app, pilot)
        await say(app, pilot, "still there?")
        await until(pilot, lambda: "You said: still there?" in screen_text(app), "the next reply")
    assert history_problems(app.agent.messages) == []


async def test_a_provider_error_is_shown_and_the_conversation_survives(tmp_path):
    app = make_app(tmp_path, replies=[])  # the fake provider has nothing to say, and raises
    async with app.run_test() as pilot:
        await say(app, pilot, "hi")
        await finished(app, pilot)
        assert "error: FakeProvider ran out of scripted replies" in screen_text(app)
        assert app.focused is app.prompt
    assert history_problems(app.agent.messages) == []


# --- Slash commands, and the prompt box ------------------------------------------------------


async def test_slash_commands_show_their_output_and_clear_empties_the_screen(tmp_path):
    app = make_app(tmp_path, ["Hello"])
    async with app.run_test() as pilot:
        await say(app, pilot, "/tools")
        await pilot.pause()
        assert "edit_file" in screen_text(app) and "shell" in screen_text(app)
        await say(app, pilot, "/nonsense")
        await pilot.pause()
        assert "unknown command /nonsense" in screen_text(app)

        await say(app, pilot, "hi")
        await finished(app, pilot)
        await say(app, pilot, "/clear")
        await pilot.pause()
        assert screen_text(app) == "Conversation cleared." and app.agent.messages == []


async def test_compact_runs_in_the_background_and_reports_what_it_saved(tmp_path):
    app = make_app(tmp_path)
    app.agent.context_budget = 100_000
    async with app.run_test() as pilot:
        await say(app, pilot, "hello")
        await finished(app, pilot)
        await say(app, pilot, "/compact")
        await until(pilot, lambda: "conversation compacted" in screen_text(app), "the compaction note")
        await finished(app, pilot)

        assert "compacting the conversation, as you asked" in screen_text(app)
        assert len(app.agent.messages) == 2 and "pretend summary" in app.agent.messages[1].text
        assert "context " in str(app.query_one("#status").content)  # the meter
        assert app.focused is app.prompt


async def test_loaded_memory_files_are_announced_and_remember_adds_to_them(tmp_path):
    (tmp_path / "home").mkdir()
    (tmp_path / "WREN.md").write_text("Be brief.\n")
    app = make_app(tmp_path, memory=Memory(tmp_path, home=tmp_path / "home"))
    async with app.run_test() as pilot:
        await until(pilot, lambda: "Instructions loaded from: WREN.md" in screen_text(app), "the announcement")
        await say(app, pilot, "/remember use tabs")
        await pilot.pause()
        assert "Added to WREN.md" in screen_text(app)
    assert (tmp_path / "WREN.md").read_text() == "Be brief.\n- use tabs\n"


async def test_the_model_picker_switches_model_and_updates_the_header(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "/model")
        await until(pilot, lambda: app.query(Picker), "the model picker")
        assert app.focused is app.query_one(Picker)
        await pilot.press("down", "enter")  # the second in the list

        await until(pilot, lambda: not app.query(Picker), "the picker to close")
        assert app.agent.provider.model == "demo-large"
        assert "demo-large" in str(app.query_one("#header").content)
        assert "Now using demo-large." in screen_text(app) and app.focused is app.prompt


async def test_the_effort_picker_sets_effort_and_shows_it_in_the_header(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "/effort")
        await until(pilot, lambda: app.query(Picker), "the effort picker")
        first = str(app.query_one(Picker).get_option_at_index(0).prompt)
        assert "default: medium for this model" in first and "(in use)" in first
        await pilot.press("down", "down", "down", "enter")  # default, low, medium, high

        await until(pilot, lambda: not app.query(Picker), "the picker to close")
        assert app.agent.provider.effort == "high"
        assert "demo (high effort)" in str(app.query_one("#header").content)
        assert "Effort is now high." in screen_text(app)

        await say(app, pilot, "hello")
        await finished(app, pilot)
        assert app.agent.provider.calls[-1]["effort"] == "high"


async def test_effort_for_a_model_without_the_setting_explains_instead_of_offering_a_list(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "/model demo-fast")
        await pilot.pause()
        assert "demo-fast" in str(app.query_one("#header").content)  # typed switches update the header too
        await say(app, pilot, "/effort")
        await until(pilot, lambda: "no separate effort setting" in screen_text(app), "the explanation")
        assert not app.query(Picker) and not app.busy


async def test_escape_closes_a_picker_without_choosing(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "/model")
        await until(pilot, lambda: app.query(Picker), "the model picker")
        await pilot.press("escape")
        await pilot.pause()
        assert not app.query(Picker) and app.agent.provider.model == "demo" and app.focused is app.prompt


async def test_resume_picks_an_earlier_conversation_and_redraws_it(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    app = make_app(tmp_path, saved=True)
    async with app.run_test() as pilot:
        await say(app, pilot, "read a.txt")
        await finished(app, pilot)
        await say(app, pilot, "/clear")
        await pilot.pause()
        await say(app, pilot, "something else entirely")
        await finished(app, pilot)
        assert "read a.txt" not in screen_text(app)

        await say(app, pilot, "/resume")
        await until(pilot, lambda: app.query(Picker), "the session picker")
        await pilot.press("enter")  # the only other conversation
        await until(pilot, lambda: "Resumed" in screen_text(app), "the old conversation")

        shown = screen_text(app)
        assert "read a.txt" in shown and "read_file(path='a.txt')" in shown  # the old turn, tool call and all
        assert "something else entirely" not in shown
        assert len(app.agent.messages) == 4 and app.focused is app.prompt
        status = str(app.query_one("#status").content)
        assert "0 in · 0 out" not in status  # the old conversation's totals came back with it


async def test_starting_with_resume_opens_the_same_picker(tmp_path):
    first = make_app(tmp_path, ["an earlier answer"], saved=True)
    async with first.run_test() as pilot:
        await say(first, pilot, "an earlier question")
        await finished(first, pilot)

    app = make_app(tmp_path, saved=True, choose_session=True)  # wren --resume
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query(Picker), "the session picker, straight away")
        assert "an earlier question" in str(app.query_one(Picker).get_option_at_index(0).prompt)
        await pilot.press("enter")
        await until(pilot, lambda: "an earlier answer" in screen_text(app), "the old conversation")


async def test_resume_with_nothing_saved_just_says_so(tmp_path):
    app = make_app(tmp_path, saved=True, choose_session=True)
    async with app.run_test() as pilot:
        await until(pilot, lambda: "no other saved conversations" in screen_text(app), "the note")
        assert not app.query(Picker)


async def test_quit_command_exits(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "/quit")
        await pilot.pause()
    assert not app.is_running


async def test_ctrl_j_adds_a_line_and_enter_sends_the_lot(tmp_path):
    app = make_app(tmp_path, ["ok"])
    async with app.run_test() as pilot:
        await pilot.press("a", "ctrl+j", "b", "enter")
        await finished(app, pilot)
    assert app.agent.messages[0].text == "a\nb"


# --- Drawing a change ---------------------------------------------------------------------------


def test_change_text_draws_a_diff_or_a_new_file(tmp_path):
    edited = change_text(FileChange(tmp_path / "a.py", "x = 1\ny = 2\n", "x = 1\ny = 3\n"), "a.py").plain
    assert "-y = 2" in edited and "+y = 3" in edited and "a/a.py" not in edited  # no ---/+++ header
    created = change_text(FileChange(tmp_path / "b.py", None, "one\ntwo\n"), "b.py").plain
    assert created == "+ one\n+ two"
    long = change_text(FileChange(tmp_path / "c.py", None, "x\n" * 100), "c.py").plain
    assert long.endswith("… and 60 more lines")
