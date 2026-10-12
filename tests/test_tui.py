"""Tests for the full-screen app.

Textual can run an app "headless": no real terminal, but the same event
loop, widgets and key handling. `app.run_test()` gives a `pilot` that
presses keys for us. So these tests type, press Enter, answer questions and
press Escape, the way a person would, and then look at what's on screen.

The tests are `async def` because the app's event loop is asyncio's;
pytest-asyncio runs them (see asyncio_mode in pyproject.toml).
"""

import os
import threading
from decimal import Decimal

import pytest
from textual.widgets import Markdown, Static

from wren.agent import INTERRUPTED_NOTE, Agent
from wren.exchange import ExchangeRate
from wren.memory import Memory
from wren.messages import Message, TextBlock, ToolUseBlock, history_problems
from wren.config import Config
from wren.providers.fake import FakeProvider
from wren.sessions import SessionStore
from wren.settings import UserDefaults
from wren.tools import Permissions, ToolRegistry, default_tools
from wren.tools.permissions import FileChange
from wren.tui import TERMINAL_LOGO, PermissionPrompt, Picker, WrenApp, change_text


def make_app(tmp_path, replies=None, memory=None, saved=False, choose_session=False, defaults=None):
    """An app on a throwaway project. With no `replies`, the fake model is in
    demo mode, where typing `read x`, `write x text` or `run cmd` asks for a tool.
    With `saved`, its conversations are written under tmp_path/home. With
    `defaults`, choices made with Enter in the pickers are kept there."""
    tools = ToolRegistry(default_tools(), root=tmp_path, permissions=Permissions())
    agent = Agent(FakeProvider(replies), "sys", 100, tools)
    sessions = None
    if saved:
        sessions = SessionStore(tmp_path, agent.provider.name, agent.provider.model, home=tmp_path / "home")
        agent.journal = sessions
    return WrenApp(agent, memory, sessions, choose_session=choose_session, defaults=defaults)


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


def footer_text(app):
    """The footer's rows, as plain text."""
    return str(app.query_one("#footer").content)


def panel_text(app):
    """What the command panel is showing ("" if it's closed)."""
    if not app.panel.is_open:
        return ""
    return "\n".join(str(widget.content) for widget in app.panel.query(Static))


def question(app):
    found = app.query(PermissionPrompt)
    return found.first() if found else None


# --- Pinned logo --------------------------------------------------------------------


async def test_logo_is_pinned_top_left_without_adding_it_to_history(tmp_path):
    app = make_app(tmp_path, ["Hello"])
    async with app.run_test(size=(80, 30)) as pilot:
        logo = app.query_one("#logo", Static)
        await until(pilot, lambda: logo.region.height == 8, "the eight-row logo to be laid out")
        assert str(logo.content) == TERMINAL_LOGO
        assert (logo.region.x, logo.region.y) == (0, 0)
        assert logo.content_region.x == 1  # the same left padding as the conversation
        assert logo.region.bottom <= app.transcript.region.y
        assert not app.transcript.query("#logo") and screen_text(app) == ""
        assert app.agent.messages == [] and app.focused is app.prompt

        await say(app, pilot, "hi")
        await finished(app, pilot)
        assert [message.text for message in app.agent.messages] == ["hi", "Hello"]
    assert history_problems(app.agent.messages) == []


async def test_logo_stays_fixed_while_chat_scrolls_and_command_panel_opens(tmp_path):
    reply = "\n\n".join(f"Paragraph {i}" for i in range(30))
    app = make_app(tmp_path, [reply])
    async with app.run_test(size=(80, 30)) as pilot:  # the smallest with the full bird
        logo = app.query_one("#logo", Static)
        await until(pilot, lambda: logo.region.height == 8, "the eight-row logo to be laid out")
        pinned_region = logo.region
        await say(app, pilot, "hi")
        await finished(app, pilot)
        await until(pilot, lambda: app.transcript.max_scroll_y > 0, "the long reply to overflow")
        assert logo.region == pinned_region

        app.transcript.scroll_home(animate=False)
        await until(pilot, lambda: app.transcript.scroll_y == 0, "the start of the conversation")
        assert logo.region == pinned_region
        app.to_the_end()
        await until(pilot, lambda: app.transcript.scroll_y == app.transcript.max_scroll_y, "the end again")
        assert logo.region == pinned_region

        await say(app, pilot, "/help")
        await until(pilot, lambda: app.panel.is_open, "the help panel")
        assert logo.region == pinned_region
        assert app.panel.region.y >= logo.region.bottom
        assert app.prompt.region.bottom <= app.screen.region.bottom
        app.prompt.insert("\n".join("Another input line" for _ in range(20)))
        await until(pilot, lambda: app.prompt.region.height == 10, "the multiline prompt to reach its cap")
        assert app.prompt.region.bottom <= app.query_one("#footer").region.y
        assert app.query_one("#footer").region.bottom <= app.screen.region.bottom  # the footer isn't pushed off
        assert app.panel.region.bottom <= app.prompt.region.y
        assert logo.region == pinned_region
        await pilot.press("escape")
        await until(pilot, lambda: not app.panel.is_open, "the help panel to close")
        assert logo.region == pinned_region and app.focused is app.prompt
    assert history_problems(app.agent.messages) == []


@pytest.mark.parametrize("size", [(18, 30), (80, 16), (80, 29)])
async def test_small_terminals_use_text_and_restore_the_bird_after_resizing(tmp_path, size):
    app = make_app(tmp_path)
    async with app.run_test(size=size) as pilot:
        logo = app.query_one("#logo", Static)
        assert str(logo.content) == "wren" and logo.region.y == 0
        await pilot.resize_terminal(80, 30)
        await until(pilot, lambda: str(logo.content) == TERMINAL_LOGO, "the bird after enlarging")
        await pilot.resize_terminal(*size)
        await until(pilot, lambda: str(logo.content) == "wren", "the compact logo after shrinking")
        assert logo.region.y == 0 and app.focused is app.prompt
        assert app.prompt.region.bottom <= app.query_one("#footer").region.y
        assert app.query_one("#footer").region.bottom <= app.screen.region.bottom


async def test_a_24_row_terminal_fits_a_full_prompt_above_the_footer(tmp_path):
    # The classic 80x24: the full bird would leave no room, so it's "wren".
    app = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        logo = app.query_one("#logo", Static)
        assert str(logo.content) == "wren"
        app.prompt.insert("\n".join("Another input line" for _ in range(20)))
        await until(pilot, lambda: app.prompt.region.height == 10, "the multiline prompt to reach its cap")
        footer = app.query_one("#footer")
        assert logo.region.bottom <= app.transcript.region.y
        assert app.transcript.region.height > 0
        assert app.prompt.region.bottom <= footer.region.y
        assert footer.region.bottom <= app.screen.region.bottom


async def test_existing_history_opens_with_the_same_pinned_logo(tmp_path):
    app = make_app(tmp_path, ["An earlier answer"])
    list(app.agent.run_turn("An earlier question"))
    async with app.run_test(size=(80, 30)) as pilot:  # tall enough for the full bird
        assert str(app.query_one("#logo", Static).content) == TERMINAL_LOGO
        assert not app.transcript.query("#logo")
        assert "An earlier answer" in screen_text(app)
        assert len(app.agent.messages) == 2
    assert history_problems(app.agent.messages) == []


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
        assert "▸ Read a.txt  → 2 lines" in screen_text(app)


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


async def test_a_command_is_asked_about_in_plain_english_with_the_command_itself_underneath(tmp_path):
    command = "grep -rl TODO src | xargs sed -i '' -e 's/TODO/DONE/g'"
    why = "Replace every TODO marker with DONE in the files under src."
    app = make_app(tmp_path, [asks(("t1", "shell", {"description": why, "command": command})), "Left alone."])
    async with app.run_test(size=(110, 30)) as pilot:
        await say(app, pilot, "tidy the markers")
        await until(pilot, lambda: question(app), "the permission question")
        shown = screen_text(app)
        assert f"Wren says: {why}" in shown  # what it's for, in words
        assert "That is Wren's description. What will actually run is:" in shown  # ...labelled as a claim
        assert f"$ {command}" in shown  # ...and never without the thing itself
        assert shown.index("Wren says:") < shown.rindex(f"$ {command}")  # description first, command under it
        await pilot.press("n")
        await finished(app, pilot)


async def test_your_messages_the_models_and_the_harnesss_are_styled_apart(tmp_path):
    (tmp_path / "a.txt").write_text("one\n")
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "read a.txt")
        await finished(app, pilot)
        yours = app.transcript.query(".user").first()
        models = app.transcript.query(".assistant").first()
        harness = app.transcript.query(".tool").first()
        assert str(yours.content).startswith("› read a.txt")
        bars = {w.styles.border_left[1].hex for w in (yours, models, harness)}
        assert len(bars) == 3  # three voices, three colours down the left edge


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


async def test_the_footer_follows_a_turn_as_it_happens(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(160, 30)) as pilot:
        assert "enter send" in footer_text(app)  # idle: the keys
        await say(app, pilot, "write a.txt one")
        await until(pilot, lambda: question(app), "the permission question")
        # The footer catches up on its next tick, a tenth of a second at most.
        await until(pilot, lambda: "running write_file(" in footer_text(app), "the footer to show the tool")
        shown = footer_text(app)
        assert "step 1/25 · running write_file(" in shown and "esc to stop" in shown
        assert "this turn: " in shown and "turns: 1" in shown

        await pilot.press("y")
        await finished(app, pilot)
        shown = footer_text(app)
        assert "tool calls: 1 · write_file 1" in shown and "files changed: 1" in shown
        assert "last turn: " in shown and "enter send" in shown
        assert "cost: <$0.01" in shown  # made-up prices, a short turn, and no exchange rate in tests


async def test_the_app_looks_up_the_days_rate_and_shows_pounds(tmp_path):
    asked = threading.Event()

    def bank():  # plays the exchange-rate service, on the worker thread
        asked.set()
        return Decimal("0.5"), "2026-10-09"

    app = make_app(tmp_path, ["Hello"])
    app.exchange = ExchangeRate(tmp_path / "home", bank)
    async with app.run_test(size=(160, 30)) as pilot:
        await until(pilot, asked.is_set, "the rate to be looked up at startup")
        await until(pilot, lambda: "cost: £0.00" in footer_text(app), "the footer to show pounds")
        saved = tmp_path / "home" / ".wren" / "exchange-rate.json"
        await until(pilot, saved.exists, "the rate to be saved for the rest of the day")


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
        # In words first, then the command as it was run, then the end of its output.
        assert "▸ Run the command you typed after 'run'.  → 2 lines\n$ echo one; echo two\none\ntwo" in screen_text(app)


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


async def test_reference_commands_open_in_a_panel_that_escape_closes(tmp_path):
    app = make_app(tmp_path, ["Hello"])
    async with app.run_test() as pilot:
        await say(app, pilot, "/tools")
        await pilot.pause()
        assert "edit_file" in panel_text(app) and "shell" in panel_text(app)
        assert screen_text(app) == ""  # nothing was added to the conversation
        assert app.panel.border_title == "/tools" and "esc to go back" in app.panel.border_subtitle
        assert app.focused is app.panel  # so the arrow keys scroll it

        await pilot.press("escape")
        await pilot.pause()
        assert not app.panel.is_open and app.focused is app.prompt
        assert screen_text(app) == ""  # and nothing was left behind


async def test_one_command_replaces_another_in_the_panel(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "/usage")
        await pilot.pause()
        assert "This conversation:" in panel_text(app)
        app.prompt.focus()
        await say(app, pilot, "/help")
        await pilot.pause()
        assert "/provider" in panel_text(app) and "This conversation:" not in panel_text(app)


async def test_escape_with_the_panel_open_closes_it_and_does_not_stop_the_turn(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "write a.txt one")
        await until(pilot, lambda: question(app), "the permission question")
        app.prompt.focus()
        await say(app, pilot, "/usage")  # looking something up while Wren waits
        await pilot.pause()
        assert app.panel.is_open

        await pilot.press("escape")
        await pilot.pause()
        assert not app.panel.is_open and app.busy and question(app) is not None  # the turn is still alive
        await pilot.press("escape")  # a second press does stop it
        await finished(app, pilot)


async def test_a_permission_question_closes_the_panel_and_takes_the_keyboard(tmp_path):
    (tmp_path / "a.txt").write_text("one\n")
    app = make_app(tmp_path, [asks(("t1", "read_file", {"path": "a.txt"})), asks(("t2", "write_file", {"path": "b.txt", "content": "x"})), "Done."])
    async with app.run_test() as pilot:
        await say(app, pilot, "/help")
        await pilot.pause()
        app.prompt.focus()
        await say(app, pilot, "go")
        await until(pilot, lambda: question(app), "the permission question")
        assert not app.panel.is_open and app.focused is question(app)
        await pilot.press("n")
        await finished(app, pilot)


async def test_slash_commands_that_change_things_leave_a_note_and_clear_empties_the_screen(tmp_path):
    app = make_app(tmp_path, ["Hello"])
    async with app.run_test(size=(80, 30)) as pilot:  # tall enough for the full bird
        logo = app.query_one("#logo", Static)
        await say(app, pilot, "/nonsense")
        await pilot.pause()
        assert "unknown command /nonsense" in screen_text(app) and not app.panel.is_open

        await say(app, pilot, "hi")
        await finished(app, pilot)
        await say(app, pilot, "/clear")
        await pilot.pause()
        assert screen_text(app) == "Conversation cleared." and app.agent.messages == []
        assert len(app.query("#logo")) == 1
        assert app.query_one("#logo", Static) is logo  # /clear did not recreate it
        assert str(logo.content) == TERMINAL_LOGO and logo.region.y == 0


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
        assert "context: " in footer_text(app)  # the meter
        assert app.stats.compactions == 1
        assert app.stats.usage == app.agent.usage  # the summary's tokens are counted too, not just the turn's
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


async def test_the_model_picker_switches_model_and_updates_the_footer(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "/model")
        await until(pilot, lambda: app.query(Picker), "the model picker")
        assert app.focused is app.query_one(Picker)
        await pilot.press("down", "enter")  # the second in the list

        await until(pilot, lambda: not app.query(Picker), "the picker to close")
        assert app.agent.provider.model == "demo-large"
        assert "model: demo-large" in footer_text(app)
        assert "Now using demo-large." in screen_text(app) and app.focused is app.prompt


async def test_the_effort_picker_sets_effort_and_shows_it_in_the_footer(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "/effort")
        await until(pilot, lambda: app.query(Picker), "the effort picker")
        first = str(app.query_one(Picker).get_option_at_index(0).prompt)
        assert "default: medium for this model" in first and "(in use)" in first
        await pilot.press("down", "down", "down", "enter")  # default, low, medium, high

        await until(pilot, lambda: not app.query(Picker), "the picker to close")
        assert app.agent.provider.effort == "high"
        assert "model: demo · effort: high" in footer_text(app)
        assert "Effort is now high." in screen_text(app)

        await say(app, pilot, "hello")
        await finished(app, pilot)
        assert app.agent.provider.calls[-1]["effort"] == "high"


# --- Enter keeps a choice for every session; Space keeps it to this one ----------------------------


async def pick(app, pilot, command, *keys):
    """Open a picker with a slash command, press `keys` in it, and wait for it to close."""
    await say(app, pilot, command)
    await until(pilot, lambda: app.query(Picker), "the picker")
    await pilot.press(*keys)
    await until(pilot, lambda: not app.query(Picker), "the picker to close")


async def test_enter_in_the_model_picker_saves_it_as_the_default_for_every_session(tmp_path):
    user = UserDefaults(tmp_path / "settings.json")
    app = make_app(tmp_path, defaults=user)
    async with app.run_test() as pilot:
        await pick(app, pilot, "/model", "down", "enter")
        assert app.agent.provider.model == "demo-large"
        assert "Saved demo-large as your default fake model" in screen_text(app)
    # Saved with the effort it has: a new model starts at its own default.
    assert user.get("fake") == ("demo-large", None)
    # The next run is not a new model choice: it starts on what was saved.
    assert Config.load(provider="fake", env={}, user=user).model == "demo-large"


async def test_space_in_the_model_picker_keeps_it_to_this_session(tmp_path):
    user = UserDefaults(tmp_path / "settings.json")
    app = make_app(tmp_path, defaults=user)
    async with app.run_test() as pilot:
        await pick(app, pilot, "/model", "down", "space")
        assert app.agent.provider.model == "demo-large"
        await until(pilot, lambda: "model: demo-large" in footer_text(app), "the footer to show the model")
        assert "Saved" not in screen_text(app)
    assert user.get("fake") is None and not user.path.exists()
    assert Config.load(provider="fake", env={}, user=user).model == "demo"


async def test_a_session_only_choice_does_not_disturb_an_earlier_saved_default(tmp_path):
    user = UserDefaults(tmp_path / "settings.json")
    app = make_app(tmp_path, defaults=user)
    async with app.run_test() as pilot:
        await pick(app, pilot, "/model", "down", "enter")  # demo-large, for good
        await pick(app, pilot, "/model", "down", "down", "space")  # demo-fast, for now
        assert app.agent.provider.model == "demo-fast"
    assert user.get("fake") == ("demo-large", None)


async def test_enter_in_the_effort_picker_saves_the_effort_with_the_default_model(tmp_path):
    user = UserDefaults(tmp_path / "settings.json")
    app = make_app(tmp_path, defaults=user)
    async with app.run_test() as pilot:
        await pick(app, pilot, "/effort", "down", "down", "down", "enter")  # default, low, medium, high
        assert app.agent.provider.effort == "high"
        assert "Saved high as your default effort with demo" in screen_text(app)
    assert user.get("fake") == ("demo", "high")
    config = Config.load(provider="fake", env={}, user=user)
    assert (config.model, config.effort) == ("demo", "high")


async def test_space_in_the_effort_picker_keeps_it_to_this_session(tmp_path):
    user = UserDefaults(tmp_path / "settings.json")
    app = make_app(tmp_path, defaults=user)
    async with app.run_test() as pilot:
        await pick(app, pilot, "/effort", "down", "down", "down", "space")
        assert app.agent.provider.effort == "high"
    assert not user.path.exists()


async def test_an_effort_chosen_with_enter_on_a_session_only_model_is_not_saved(tmp_path):
    # The saved effort is for the default model; this model isn't it.
    user = UserDefaults(tmp_path / "settings.json")
    app = make_app(tmp_path, defaults=user)
    async with app.run_test() as pilot:
        await pick(app, pilot, "/model", "down", "space")  # demo-large, for now
        await pick(app, pilot, "/effort", "down", "down", "down", "enter")
        assert app.agent.provider.effort == "high"  # applied to the session all the same
        assert "for this session only" in screen_text(app)
    assert not user.path.exists()


async def test_enter_on_the_model_already_in_use_saves_it_with_the_effort_it_has(tmp_path):
    user = UserDefaults(tmp_path / "settings.json")
    app = make_app(tmp_path, defaults=user)
    async with app.run_test() as pilot:
        app.agent.set_effort("low")
        await pick(app, pilot, "/model", "enter")  # "demo", already in use
        assert "Already using demo" in screen_text(app)
    assert user.get("fake") == ("demo", "low")


async def test_typed_commands_never_save_a_default(tmp_path):
    user = UserDefaults(tmp_path / "settings.json")
    app = make_app(tmp_path, defaults=user)
    async with app.run_test() as pilot:
        await say(app, pilot, "/model demo-large")
        await say(app, pilot, "/effort high")
        await pilot.pause()
        assert (app.agent.provider.model, app.agent.provider.effort) == ("demo-large", "high")
    assert not user.path.exists()


async def test_the_model_and_effort_pickers_say_what_the_two_keys_do(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "/model")
        await until(pilot, lambda: app.query(Picker), "the model picker")
        assert "enter choose" not in app.panel.border_subtitle
        assert "enter set as default" in app.panel.border_subtitle and "space this session only" in app.panel.border_subtitle


async def test_effort_for_a_model_without_the_setting_explains_instead_of_offering_a_list(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await say(app, pilot, "/model demo-fast")
        await pilot.pause()
        assert "model: demo-fast" in footer_text(app)  # typed switches update the footer too
        await say(app, pilot, "/effort")
        await until(pilot, lambda: "no separate effort setting" in screen_text(app), "the explanation")
        assert not app.query(Picker) and not app.busy


async def test_the_provider_picker_switches_provider_and_says_which_need_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    app = make_app(tmp_path)  # on the fake provider
    async with app.run_test() as pilot:
        await say(app, pilot, "/provider")
        await until(pilot, lambda: app.query(Picker), "the provider picker")
        rows = [str(app.query_one(Picker).get_option_at_index(i).prompt) for i in range(3)]
        assert "anthropic" in rows[0] and "claude-opus-5-5" in rows[0]
        assert "openai" in rows[1] and "needs OPENAI_API_KEY" in rows[1]
        assert "fake" in rows[2] and "in use" in rows[2]

        await pilot.press("down", "enter")  # openai: no key
        await until(pilot, lambda: "needs an API key" in screen_text(app), "the refusal")
        assert app.agent.provider.name == "fake"  # nothing changed

        await say(app, pilot, "/provider")
        await until(pilot, lambda: app.query(Picker), "the provider picker")
        await pilot.press("enter")  # anthropic
        await until(pilot, lambda: "Now using anthropic" in screen_text(app), "the switch")
        assert app.agent.provider.name == "anthropic"
        shown = "model: claude-opus-5-5 · effort: xhigh · provider: anthropic"
        await until(pilot, lambda: shown in footer_text(app), "the footer to show the new provider")


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
    async with app.run_test(size=(250, 30)) as pilot:  # wide, so the footer has room for the session id
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
        assert "read a.txt" in shown and "▸ Read a.txt" in shown  # the old turn, tool call and all
        assert "something else entirely" not in shown
        assert len(app.agent.messages) == 4 and app.focused is app.prompt
        assert app.agent.usage.sent_tokens > 0  # the old conversation's totals came back with it (/usage shows them)
        assert f"session id: {app.sessions.current_id} (resumed)" in footer_text(app)


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
