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
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message as TextualMessage
from textual.widget import Widget
from textual.widgets import Markdown, Static, TextArea

from wren import __version__
from wren.agent import Agent, ToolFinished, ToolStarted, TurnFinished
from wren.commands import format_call, run_command
from wren.messages import Completed, TextDelta, ToolUseBlock
from wren.tools import Decision, PermissionRequest, ToolResult
from wren.tools.files import diff

MAX_DIFF_LINES = 40  # of a diff or new file, in a permission question or a result
OUTPUT_TAIL_LINES = 10  # of a command's output, under its tool call

KEYS_HELP = """\
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


# --- The app ----------------------------------------------------------------------


class WrenApp(App):
    TITLE = "wren"

    # Textual styles widgets with CSS, like a web page. `$accent` and friends
    # are colours from the current theme, so this follows light and dark.
    CSS = """
    #header { height: 1; padding: 0 1; background: $panel; color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis; }
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
    """

    BINDINGS = [
        Binding("escape", "stop", "Stop Wren", priority=True),
        Binding("ctrl+d", "quit", "Quit"),
    ]

    def __init__(self, agent: Agent) -> None:
        super().__init__()
        self.agent = agent
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
        provider, tools = self.agent.provider, self.agent.tools
        where = str(tools.context.root).replace(str(Path.home()), "~", 1)
        yield Static(
            f"wren {__version__} · {provider.name} · {provider.model} · {len(tools)} tools · {where}",
            id="header",
            markup=False,
        )
        yield VerticalScroll(id="transcript")
        yield Static("", id="status", markup=False)
        yield Prompt(id="prompt", placeholder="Ask Wren something, or /help", show_line_numbers=False)

    def on_mount(self) -> None:
        # "Anchored" means: stay scrolled to the bottom as content is added,
        # until the user scrolls up to read something.
        self.transcript.anchor()
        self.set_interval(0.1, self._tick)
        self._show_status()
        self.prompt.focus()

    @property
    def transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    @property
    def prompt(self) -> Prompt:
        return self.query_one("#prompt", Prompt)

    async def add(self, widget: Widget) -> Widget:
        await self.transcript.mount(widget)
        return widget

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
            self.busy = True
            self._stop.clear()
            self._show_status()
            self.run_turn(text)

    async def run_slash(self, line: str) -> None:
        if self.busy and line.split()[0].lower() == "/clear":
            self.notify("Stop Wren (Escape) before clearing the conversation.")
            return
        result = run_command(self.agent, line, KEYS_HELP)
        if result.quit:
            self.action_quit_wren()
            return
        if result.cleared:
            await self.transcript.remove_children()
        if result.text:
            await self.note(result.text, "error" if result.is_error else "output")

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
        self._question = PermissionPrompt(request, answer)
        await self.add(self._question)
        self._question.focus()

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

    def action_stop(self) -> None:
        """Escape. Stopping is cooperative: the worker notices at its next
        event, and a running command within a fraction of a second."""
        if self.busy and not self._stop.is_set():
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
        usage = self.agent.usage
        tokens = f"{usage.input_tokens:,} in · {usage.output_tokens:,} out"
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
