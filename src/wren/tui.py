"""The full-screen front end, built on Textual.

What you see when you run `wren`: the conversation scrolling in the middle,
a box to type in at the bottom, and permission questions appearing in the
conversation where they come up.

This file does the same job as repl.py: turn the agent's events into
something on screen, and answer its permission questions. The agent is
unchanged. What's different is the kind of program this is.

  * The REPL is a straight line. It calls input(), then runs the turn, and
    while the turn runs nothing else can happen.

  * A Textual app is a loop that must never stop. It redraws the screen,
    notices keys and mouse, and runs small pieces of our code ("handlers")
    in between. Each handler has to finish quickly, or the whole screen
    freezes: no spinner, no scrolling, no Escape.

But the agent is slow, on purpose: it waits on the network and on commands.
So a turn runs on a second thread (a "worker"), and the two threads follow
one rule: ONLY THE APP'S OWN THREAD TOUCHES THE SCREEN. The worker never
changes a widget. It asks the app to, with `call_from_thread`, and waits.

    worker thread                         app thread (the event loop)
    -------------                         ---------------------------
    for event in agent.run_turn(text):
        call_from_thread(show_event)  ->  mount / update widgets
    ...
    ask_permission(request)           ->  show the question
        (sleeps until answered)       <-  you press y / a / n
"""

from __future__ import annotations

import concurrent.futures
import threading
from contextlib import closing
from importlib.resources import files
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.geometry import Size
from textual.message import Message as TextualMessage
from textual.widget import Widget
from textual.widgets import Markdown, OptionList, Static, TextArea

from wren import __version__
from wren.agent import Agent, Compacted, CompactionStarted, ToolFinished, ToolStarted, TurnFinished
from wren.commands import (
    budget_used,
    compaction_note,
    default_effort,
    effort_list,
    format_call,
    model_list,
    provider_choices,
    resume_session,
    run_command,
    session_label,
    shorten,
    switch_effort,
    switch_model,
    switch_provider,
)
from wren.memory import Memory
from wren.messages import Completed, Message, TextDelta, ToolUseBlock
from wren.prompts import COMPACTED_NOTE
from wren.sessions import SessionError, SessionStore
from wren.tools import Decision, PermissionRequest, ToolResult
from wren.tools.files import diff

# Package data, not a path into docs: the installed app can run in any folder.
TERMINAL_LOGO = files("wren").joinpath("wren-terminal.txt").read_text(encoding="utf-8").rstrip("\n")

MAX_DIFF_LINES = 40  # of a diff or new file, in a permission question or a result
OUTPUT_TAIL_LINES = 10  # of a command's output, under its tool call

KEYS_HELP = """\
Commands open in a panel above the prompt. Escape closes it.
Enter sends your message; Ctrl+J starts a new line in it.
Wren asks before it edits a file or runs a command: y, n, or a for "always".
Escape stops Wren while it's working. Whatever it had already done stays done,
and stays in the conversation. Ctrl+Q quits.
Select text with the mouse and press Ctrl+C to copy it."""

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# --- Widgets: the pieces of the conversation -------------------------------------
#
# Everything shown is a "widget": an object that knows how to draw itself and
# can be styled with CSS (see WrenApp.CSS). Text that came from outside (the
# model, a file, a command) is always passed as a Rich `Text` object or with
# markup=False, because Textual would otherwise read "[bold]" in a plain
# string as an instruction to itself.


class Prompt(TextArea):
    """The box you type in. A TextArea is a small multi-line editor; we
    change what one key does: Enter sends, where it would add a line."""

    class Submitted(TextualMessage):
        """Posted when you press Enter. Widgets talk to the app by posting
        messages like this; the app handles it in `WrenApp.submitted`."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()  # don't insert the line break
            event.stop()
            text = self.text.strip()
            if text:
                self.post_message(self.Submitted(text))
        elif event.key == "ctrl+j":
            event.prevent_default()
            event.stop()
            self.insert("\n")
        else:
            await super()._on_key(event)


class ToolCallView(Static):
    """One tool call: a line saying what was asked, then what came of it."""

    def __init__(self, call: ToolUseBlock) -> None:
        self.call_text = format_call(call)
        super().__init__(self._line("…", "dim"), markup=False, classes="tool")

    def _line(self, outcome: str, style: str) -> Text:
        return Text.assemble(("▸ ", "cyan"), (self.call_text, "dim"), "  ", (outcome, style))

    def finish(self, call: ToolUseBlock, result: ToolResult) -> None:
        """Show the result: a summary always, plus the detail worth seeing."""
        if result.is_error:
            shown = self._line(f"✗ {result.summary}", "red")
        else:
            shown = self._line(f"→ {result.summary}", "dim")
            if result.change is not None:  # an edit: show what changed
                shown = Text("\n").join([shown, change_text(result.change, Path(call.input.get("path", "")).name)])
            elif call.name == "shell":  # a command: show the end of its output
                shown = Text("\n").join([shown, Text(_tail(result.content, OUTPUT_TAIL_LINES), style="dim")])
        self.update(shown)


class PermissionPrompt(Widget, can_focus=True):
    """A question in the conversation: may Wren do this? Answered with a key.

    It takes the keyboard focus while it's showing, which is what makes "y"
    an answer here and a letter in the prompt box the rest of the time:
    a key press goes to whichever widget has the focus.
    """

    BINDINGS = [
        Binding("y", "answer('allow')", "Yes"),
        Binding("a", "answer('always')", "Always"),
        Binding("n", "answer('deny')", "No"),
    ]

    def __init__(self, request: PermissionRequest, answer: concurrent.futures.Future) -> None:
        super().__init__(classes="permission")
        self.request = request
        self.answer = answer

    def compose(self) -> ComposeResult:
        request = self.request
        yield Static(Text(request.title, style="bold yellow"), markup=False)
        if request.change is not None:
            yield Static(change_text(request.change, request.change.path.name), markup=False, classes="detail")
        else:
            yield Static(Text(request.detail), markup=False, classes="detail")
        choices = Text.assemble(("y", "bold"), " yes   ")
        if request.scope is not None:
            choices.append_text(Text.assemble(("a", "bold"), f" always allow {request.scope_label} this session   "))
        choices.append_text(Text.assemble(("n", "bold"), " no   ", ("esc", "bold"), " stop"))
        yield Static(choices, markup=False)

    def action_answer(self, choice: str) -> None:
        if choice == "always" and self.request.scope is None:
            return  # "always" wasn't offered for this one
        if not self.answer.done():
            self.answer.set_result(Decision(choice))  # wakes the worker thread
        self.app.query_one("#prompt").focus()  # hand the keyboard back
        self.remove()


class Picker(OptionList):
    """A list to choose one thing from: a provider, a model, an effort
    level, a saved conversation. Arrow keys move, Enter chooses.

    It's Textual's own OptionList, plus a note of what each row stands for
    and what to do with the choice. It's shown in the command panel.
    """

    def __init__(self, rows: list[tuple[str, Any]], on_choice) -> None:
        # Text objects, not strings: the labels come from outside (a model
        # id, the first line of an old conversation) and mustn't be read as markup.
        super().__init__(*[Text(label) for label, _ in rows], classes="picker")
        self.values = [value for _, value in rows]
        self.on_choice = on_choice


class CommandPanel(VerticalScroll):
    """Where a slash command shows its output: a box that opens above the
    prompt, and closes with Escape.

    Commands like /usage or /history are you asking Wren about itself. Their
    answers aren't part of the conversation with the model, so they don't go
    in the transcript, where they'd pile up between the messages that are.
    The panel shows one thing at a time and leaves nothing behind.

    It's always there, just hidden: opening it means filling it and adding
    the CSS class that makes it visible.
    """

    def __init__(self) -> None:
        super().__init__(id="panel")
        self.border_subtitle = "esc to go back"

    @property
    def is_open(self) -> bool:
        return self.has_class("open")

    async def show(self, title: str, content: Widget, keys: str = "↑↓ scroll") -> None:
        await self.remove_children()
        await self.mount(content)
        self.border_title = title
        self.border_subtitle = f"{keys} · esc to go back"
        self.add_class("open")
        self.scroll_home(animate=False)
        # The keyboard goes to the content if it wants it (a picker), and
        # otherwise to the panel itself, so the arrow keys scroll the text.
        (content if content.can_focus else self).focus()

    async def hide(self) -> None:
        self.remove_class("open")
        await self.remove_children()


# --- The app ----------------------------------------------------------------------


class WrenApp(App):
    TITLE = "wren"

    # Textual styles widgets with CSS, like a web page. `$accent` and friends
    # are colours from the current theme, so this follows light and dark.
    CSS = """
    #logo { dock: top; height: auto; padding: 0 1; color: $accent; text-wrap: nowrap; }
    #header { height: 1; padding: 0 1; background: $panel; color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis; }
    #body { height: 1fr; }
    #chat { height: 1fr; }
    #transcript { height: 1fr; padding: 0 1; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    #prompt { height: auto; max-height: 10; border: round $accent; }
    .user { margin: 1 0 0 0; padding: 0 1; background: $boost; color: $text; }
    .assistant { margin: 1 0 0 0; padding: 0; background: transparent; }
    .tool { margin: 0 0 0 2; }
    .note { margin: 1 0 0 0; color: $text-muted; }
    .error { margin: 1 0 0 0; color: $error; }
    .output { margin: 1 0 0 0; }
    .permission { height: auto; margin: 1 0 0 2; padding: 0 1; border: round $warning; }
    .permission:focus { border: heavy $warning; }
    .permission .detail { margin: 0 0 1 0; }
    #panel { display: none; height: auto; max-height: 60%; padding: 0 1; border: round $accent; }
    #panel.open { display: block; }
    #panel:focus-within { border: heavy $accent; }
    .picker { height: auto; border: none; padding: 0; background: transparent; }
    """

    BINDINGS = [
        Binding("escape", "stop", "Stop Wren", priority=True),
        Binding("ctrl+d", "quit", "Quit"),
    ]

    def __init__(
        self,
        agent: Agent,
        memory: Memory | None = None,
        sessions: SessionStore | None = None,
        notice: str = "",
        choose_session: bool = False,
    ) -> None:
        super().__init__()
        self.agent = agent
        self.memory = memory
        self.sessions = sessions
        self.notice = notice  # something to say at startup, e.g. which session was resumed
        self.choose_session = choose_session  # started with --resume: open the session picker first
        self.busy = False  # True while a turn is running
        # Set (by Escape) to ask the worker thread to stop. A threading.Event
        # is a flag that's safe for one thread to set and another to read.
        self._stop = threading.Event()
        self._reply: Markdown | None = None  # the assistant message being streamed, if any
        self._stream: Any = None  # ...and Textual's helper for appending to it
        self._tool_views: dict[str, ToolCallView] = {}
        self._question: PermissionPrompt | None = None
        self._frame = 0

        # The two hooks the tools layer offers a front end (see cli.py for
        # the REPL's version of the first).
        agent.tools.permissions.ask = self.ask_permission
        agent.tools.context.should_stop = self._stop.is_set

    def compose(self) -> ComposeResult:
        """Say what's on screen, top to bottom. Called once, at startup."""
        # A screen-level widget, outside scrollback and history redraws.
        yield Static(self._logo_text(), id="logo", markup=False)
        yield Static(self._header_text(), id="header", markup=False)
        # The panel shares only the space left after reserving the pinned
        # logo, status and prompt, even when the prompt grows to several lines.
        with Vertical(id="body"):
            with Vertical(id="chat"):
                yield VerticalScroll(id="transcript")
                yield CommandPanel()
            yield Static("", id="status", markup=False)
            yield Prompt(id="prompt", placeholder="Ask Wren something, or /help", show_line_numbers=False)

    def _header_text(self) -> str:
        provider, tools = self.agent.provider, self.agent.tools
        where = str(tools.context.root).replace(str(Path.home()), "~", 1)
        effort = f" ({provider.effort} effort)" if provider.effort else ""
        return f"wren {__version__} · {provider.name} · {provider.model}{effort} · {len(tools)} tools · {where}"

    async def on_mount(self) -> None:
        # "Anchored" means: stay scrolled to the bottom as content is added,
        # until the user scrolls up to read something.
        self.transcript.anchor()
        self.set_interval(0.1, self._tick)
        self._show_status()
        self.prompt.focus()
        await self._draw_conversation()

    def on_unmount(self) -> None:
        # However the app comes to close, release a worker that's waiting
        # on a question or a command, so the program can actually exit.
        self._stop.set()

    def _logo_text(self, size: Size | None = None) -> str:
        # Don't squeeze an eight-row bird into a short or narrow terminal.
        size = self.size if size is None else size
        if size.width < 20 or size.height < 20:
            return "wren"
        return TERMINAL_LOGO

    def on_resize(self, event: events.Resize) -> None:
        # Our handler runs before Textual updates the app's stored size.
        for logo in self.query("#logo"):
            logo.update(self._logo_text(event.size))

    async def _draw_conversation(self) -> None:
        """Redraw history without touching the pinned logo."""
        await self.transcript.remove_children()
        await self._announce_memory()
        if self.notice:
            await self.note(self.notice)
            self.notice = ""
        pending: dict[str, ToolCallView] = {}  # tool calls waiting for the result in the next message
        for message in self.agent.messages:
            await self._draw_message(message, pending)
        self._show_header()
        self._show_status()
        self.to_the_end()  # a resumed conversation opens at its latest message
        if self.choose_session:
            self.choose_session = False
            await self.pick_session()

    async def _draw_message(self, message: Message, pending: dict[str, ToolCallView]) -> None:
        if message.role == "user":
            for result in message.tool_results:
                view = pending.pop(result.tool_use_id, None)
                if view is not None:
                    # The one-line summaries weren't saved, only what the
                    # model saw. Its first line is the next best thing.
                    first_line = shorten((result.content.strip().splitlines() or [""])[0], 70)
                    view.update(view._line(("✗ " if result.is_error else "→ ") + first_line, "red" if result.is_error else "dim"))
            if message.text == COMPACTED_NOTE:
                await self.note(message.text)
            elif message.text:
                await self.add(Static(message.text, markup=False, classes="user"))
        else:
            if message.text:
                await self.add(Markdown(message.text, classes="assistant"))
            for call in message.tool_uses:
                pending[call.id] = ToolCallView(call)
                await self.add(pending[call.id])

    async def _announce_memory(self) -> None:
        # Said up front, every time: these files steer the model, and in a
        # project you've just cloned you didn't write them.
        if self.memory and self.memory.files:
            names = ", ".join(self.memory.shown(file.path) for file in self.memory.files)
            await self.note(f"Instructions loaded from: {names}  (/memory for details)")

    @property
    def transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    @property
    def panel(self) -> CommandPanel:
        return self.query_one("#panel", CommandPanel)

    @property
    def prompt(self) -> Prompt:
        return self.query_one("#prompt", Prompt)

    async def add(self, widget: Widget) -> Widget:
        await self.transcript.mount(widget)
        return widget

    async def ask(self, widget: Widget) -> None:
        """Put a widget that needs an answer (a permission question, a
        picker) at the end of the conversation, give it the keyboard, and
        make sure it can be seen.

        The obvious way, mount it and call focus(), has a trap. Focusing a
        widget scrolls it into view, and at that moment the new widget
        hasn't been laid out yet: as far as Textual knows, it's at the very
        top. So the conversation jumped to its beginning, and the question
        was left off-screen at the bottom, waiting. Scrolling by hand also
        cancels the "stay at the bottom" anchor, so it stayed wrong.

        So: take the focus WITHOUT scrolling, then pin the conversation to
        its end again, once now and once after the layout has caught up.
        """
        await self.transcript.mount(widget)
        widget.focus(scroll_visible=False)
        self.to_the_end()

    def to_the_end(self) -> None:
        """Scroll the conversation to its end, and keep it there as more
        arrives (until the user scrolls away)."""
        self.transcript.anchor()
        self.call_after_refresh(self.transcript.scroll_end, animate=False)

    async def note(self, text: str, classes: str = "note") -> None:
        await self.add(Static(text, markup=False, classes=classes))

    # --- Input ---------------------------------------------------------------

    @on(Prompt.Submitted)
    async def submitted(self, message: Prompt.Submitted) -> None:
        text = message.text
        if text.startswith("/"):
            self.prompt.clear()
            await self.run_slash(text)
        elif self.busy:
            self.notify("Wren is still working. Press Escape to stop it first.")
        else:
            self.prompt.clear()
            await self.add(Static(text, markup=False, classes="user"))
            self.to_the_end()  # you've spoken: come back to the present, wherever you'd scrolled to
            self.busy = True
            self._stop.clear()
            self._show_status()
            self.run_turn(text)

    async def run_slash(self, line: str) -> None:
        name = line.split()[0].lower()
        if self.busy and name in ("/clear", "/compact", "/provider", "/model", "/effort", "/resume"):
            self.notify("Wren is still working. Press Escape to stop it, or try that when it has finished.")
            return
        await self.close_panel()  # one command's output at a time
        result = run_command(self.agent, line, KEYS_HELP, self.memory, self.sessions)

        if result.quit:
            self.action_quit_wren()
        elif result.pick_provider:
            await self.pick_provider()
        elif result.pick_model:
            self.busy = True  # fetching the list is a network call: off to the worker
            self._show_status()
            self.fetch_models()
        elif result.pick_effort:
            self.busy = True  # another question for the API
            self._show_status()
            self.fetch_efforts()
        elif result.pick_session:
            await self.pick_session()
        elif result.resumed:
            self.notice = result.text
            await self._draw_conversation()
        elif result.compact:
            self.busy = True
            self._stop.clear()
            self._show_status()
            self.run_compact()
        elif result.panel and not result.is_error:
            # Reference output (/usage, /history...): shown in the panel,
            # and gone when you press Escape.
            await self.panel.show(line, Static(result.text, markup=False))
        else:
            # Something that changed the conversation or its settings
            # (/clear, /remember, /model NAME), or went wrong: a note in
            # the transcript, so there's a record of it where it happened.
            if result.cleared:
                await self._draw_conversation()  # empty now, with memory re-read for the new conversation
            if result.text:
                await self.note(result.text, "error" if result.is_error else "output")
        self._show_header()  # /provider, /model NAME and /effort LEVEL change it

    async def close_panel(self) -> None:
        if self.panel.is_open:
            await self.panel.hide()
            self.prompt.focus()

    def _show_header(self) -> None:
        self.query_one("#header", Static).update(self._header_text())

    # --- A turn, on the worker thread -------------------------------------------

    @work(thread=True, exclusive=True, group="turn")
    def run_turn(self, text: str) -> None:
        """Run one turn of the agent. THIS METHOD RUNS ON THE WORKER THREAD:
        it must not touch widgets, only ask the app to (call_from_thread)."""
        error: Exception | None = None
        try:
            # Leaving the `closing` block early closes the generator, which
            # makes the agent tidy its history (step 3). So Escape here is
            # the same thing as Ctrl-C in the REPL.
            with closing(self.agent.run_turn(text)) as turn:
                for event in turn:
                    if self._stop.is_set():
                        break
                    self._on_app_thread(self.show_event, event)
        except Exception as e:  # network trouble, a bad API key...
            error = e
        self._on_app_thread(self.turn_ended, error)

    @work(thread=True, exclusive=True, group="turn")
    def run_compact(self) -> None:
        """/compact: a model call, so it runs on the worker thread like a turn."""
        error: Exception | None = None
        try:
            with closing(self.agent.compact()) as events:
                for event in events:
                    if self._stop.is_set():
                        break
                    self._on_app_thread(self.show_event, event)
        except Exception as e:
            error = e
        self._on_app_thread(self.turn_ended, error)

    # --- Pickers: choosing a provider, a model, an effort or a saved conversation ----

    async def pick_provider(self) -> None:
        await self._open_picker("/provider", provider_choices(self.agent), self._provider_chosen)

    async def _provider_chosen(self, name: str) -> None:
        text, ok = switch_provider(self.agent, name)
        await self.note(text, "output" if ok else "error")
        self._show_header()

    @work(thread=True, exclusive=True, group="turn")
    def fetch_models(self) -> None:
        models, problem = model_list(self.agent)  # may ask the API: worker thread
        self._on_app_thread(self.pick_model, models, problem)

    async def pick_model(self, models: list[str], problem: str) -> None:
        self.busy = False
        self._show_status()
        if problem:
            await self.note(problem, "error")
            return
        current = self.agent.provider.model
        rows = [(model + ("   (in use)" if model == current else ""), model) for model in models]
        await self._open_picker(f"/model · {self.agent.provider.name}", rows, self._model_chosen)

    async def _model_chosen(self, model: str) -> None:
        await self.note(switch_model(self.agent, model))
        self._show_header()

    @work(thread=True, exclusive=True, group="turn")
    def fetch_efforts(self) -> None:
        levels, problem = effort_list(self.agent)  # may ask the API: worker thread
        self._on_app_thread(self.pick_effort, levels, problem)

    async def pick_effort(self, levels: list[str], problem: str) -> None:
        self.busy = False
        self._show_status()
        if problem:  # including "this model has no separate effort setting"
            await self.note(problem)
            return
        current = self.agent.provider.effort
        usual = default_effort(self.agent)
        means = f": {usual} for this model" if usual else ": let the model decide"
        rows = [(f"default{means}" + ("   (in use)" if current is None else ""), None)]
        rows += [(level + ("   (in use)" if level == current else ""), level) for level in levels]
        await self._open_picker(f"/effort · {self.agent.provider.model}", rows, self._effort_chosen)

    async def _effort_chosen(self, effort: str | None) -> None:
        await self.note(switch_effort(self.agent, effort))
        self._show_header()

    async def pick_session(self) -> None:
        saved = [s for s in self.sessions.saved() if s.id != self.sessions.current_id] if self.sessions else []
        if not saved:
            await self.note("There are no other saved conversations for this folder.")
            return
        rows = [(session_label(session), session) for session in saved[:50]]
        await self._open_picker("/resume", rows, self._session_chosen)

    async def _session_chosen(self, session) -> None:
        try:
            self.notice = resume_session(self.agent, self.sessions, session)
        except (SessionError, ValueError) as e:
            await self.note(f"couldn't resume: {e}", "error")
            return
        await self._draw_conversation()

    async def _open_picker(self, title: str, rows: list, on_choice) -> None:
        await self.panel.show(title, Picker(rows, on_choice), keys="↑↓ move · enter choose")

    @on(OptionList.OptionSelected)
    async def _picked(self, event: OptionList.OptionSelected) -> None:
        picker = event.option_list
        if isinstance(picker, Picker):
            value, on_choice = picker.values[event.option_index], picker.on_choice
            await self.close_panel()
            await on_choice(value)

    def _on_app_thread(self, method, *args) -> None:
        """Run `method` on the app's thread and wait for it to finish.
        Waiting matters: it keeps events in order, and it stops a fast model
        from queueing updates faster than the screen can draw them."""
        try:
            self.call_from_thread(method, *args)
        except RuntimeError:  # the app has shut down (you quit mid-turn): nothing to draw on
            self._stop.set()

    def ask_permission(self, request: PermissionRequest) -> Decision:
        """Called by the tools layer, ON THE WORKER THREAD, mid-tool-call.

        A Future is a box for one value that doesn't exist yet. We hand it to
        the app thread along with the question, and sleep here until a key
        press puts the answer in it.
        """
        answer: concurrent.futures.Future = concurrent.futures.Future()
        self._on_app_thread(self.show_question, request, answer)
        while True:
            try:
                return answer.result(timeout=0.1)
            except concurrent.futures.TimeoutError:
                if self._stop.is_set():  # Escape at the question: stop the turn, change nothing
                    self._on_app_thread(self.hide_question)
                    return Decision.DENY

    # --- Showing things, on the app thread ----------------------------------------

    async def show_event(self, event: Any) -> None:
        if isinstance(event, TextDelta):
            if self._reply is None:
                self._reply = Markdown("", classes="assistant")
                await self.add(self._reply)
                # Re-rendering all the markdown for every word would crawl on
                # a long reply. Textual's stream appends to what's there.
                self._stream = Markdown.get_stream(self._reply)
            await self._stream.write(event.text)

        elif isinstance(event, Completed):
            await self._end_reply()
            if event.stop_reason == "max_tokens":
                await self.note("[reply cut off: hit the max_tokens limit]", "error")

        elif isinstance(event, ToolStarted):
            await self._end_reply()
            view = ToolCallView(event.call)
            self._tool_views[event.call.id] = view
            await self.add(view)

        elif isinstance(event, ToolFinished):
            view = self._tool_views.pop(event.call.id, None)
            if view is not None:
                view.finish(event.call, event.result)

        elif isinstance(event, CompactionStarted):
            why = "it passed the context budget" if event.automatic else "as you asked"
            await self.note(f"[compacting the conversation, {why}…]")

        elif isinstance(event, Compacted):
            await self.note(compaction_note(event), "error" if event.error else "note")

        elif isinstance(event, TurnFinished):
            if event.hit_step_limit:
                await self.note(
                    f"[stopped after {event.steps} model calls, the limit for one turn: say 'continue' to carry on]",
                    "error",
                )

    async def _end_reply(self) -> None:
        if self._stream is not None:
            await self._stream.stop()  # writes anything still queued
        self._reply = self._stream = None

    async def show_question(self, request: PermissionRequest, answer: concurrent.futures.Future) -> None:
        await self.close_panel()  # a question needs the keyboard, and your attention
        self._question = PermissionPrompt(request, answer)
        await self.ask(self._question)

    def hide_question(self) -> None:
        if self._question is not None and self._question.is_attached:
            self._question.remove()
        self._question = None

    async def turn_ended(self, error: Exception | None) -> None:
        await self._end_reply()
        self.hide_question()
        for view in self._tool_views.values():  # tool calls the stop cut short
            view.update(view._line("✗ stopped", "red"))
        self._tool_views.clear()
        if error is not None:
            await self.note(f"error: {error}", "error")
            await self.note("The turn stopped there. What happened before the error is kept in the conversation.")
        elif self._stop.is_set():
            await self.note("[stopped: anything Wren had already done is kept, and it knows where it stopped]")
        self.busy = False
        self._show_status()
        self.prompt.focus()

    # --- Keys, status and quitting ---------------------------------------------------

    async def action_stop(self) -> None:
        """Escape. Stopping is cooperative: the worker notices at its next
        event, and a running command within a fraction of a second."""
        if self.panel.is_open:
            # Escape with the command panel open goes back to the chat. It
            # doesn't also stop a running turn: one press, one meaning.
            await self.close_panel()
        elif self.busy and not self._stop.is_set():
            self._stop.set()
            self._show_status()

    def action_quit_wren(self) -> None:
        self._stop.set()  # let a running turn tidy up
        self.exit()

    async def action_quit(self) -> None:
        self.action_quit_wren()

    def _tick(self) -> None:
        if self.busy:
            self._frame += 1
            self._show_status()

    def _show_status(self) -> None:
        agent = self.agent
        tokens = f"{agent.usage.sent_tokens:,} in · {agent.usage.output_tokens:,} out"
        if agent.context_budget:
            # The meter: how close the conversation is to being compacted.
            tokens = f"context {budget_used(agent)} · {tokens}"
        if not self.busy:
            left = "enter send · ctrl+j new line · /help · ctrl+q quit"
        elif self._stop.is_set():
            left = f"{SPINNER[self._frame % len(SPINNER)]} stopping…"
        else:
            left = f"{SPINNER[self._frame % len(SPINNER)]} working · esc to stop"
        self.query_one("#status", Static).update(f"{left}    {tokens}")


# --- Small helpers ------------------------------------------------------------------


def change_text(change, name: str) -> Text:
    """A file change as coloured text: a diff, or the content of a new file."""
    if change.before is None:
        lines = change.after.splitlines()
        colour = "green"
        shown = [("+ " + line, colour) for line in lines[:MAX_DIFF_LINES]]
    else:
        lines = diff(change.before, change.after, name).splitlines()[2:]  # drop the ---/+++ header
        shown = [(line, _diff_style(line)) for line in lines[:MAX_DIFF_LINES]]
    text = Text("\n").join(Text(line, style=style) for line, style in shown)
    if len(lines) > MAX_DIFF_LINES:
        text.append(f"\n… and {len(lines) - MAX_DIFF_LINES:,} more lines", style="dim")
    return text


def _diff_style(line: str) -> str:
    if line.startswith("+"):
        return "green"
    if line.startswith("-"):
        return "red"
    return "cyan" if line.startswith("@@") else ""


def _tail(text: str, count: int) -> str:
    lines = text.splitlines()
    if len(lines) <= count:
        return "\n".join(lines)
    return "\n".join([f"… {len(lines) - count:,} earlier lines", *lines[-count:]])
