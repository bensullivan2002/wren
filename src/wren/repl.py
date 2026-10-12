"""The plain front end: a REPL (Read, Eval, Print, Loop).

    read a line -> run a turn on the agent -> print events as they stream -> repeat

This was Wren's only interface until step 4. The full-screen app (tui.py) is
now what you get by default; this one remains for `wren --plain`, and for
whenever Wren isn't attached to a real terminal (a pipe, a script, the
tests). It needs nothing but print() and input().

Everything about *showing* things lives here, and nothing about *doing*
things. The agent hands us events; we decide how they look. Swapping this
file for a fancier interface later shouldn't require touching the agent.

The same goes for permission prompts. The tools layer decides *whether* to
ask (tools/permissions.py); `Repl.ask_permission` decides what asking looks
like.
"""

from __future__ import annotations

import os
import sys
from contextlib import closing
from typing import Any, Callable, TextIO

from wren import __version__
from wren.agent import Agent, Compacted, CompactionStarted, ToolFinished, ToolStarted, TurnFinished
from wren.commands import (
    compaction_note,
    default_effort,
    effort_list,
    format_call,
    model_list,
    provider_choices,
    run_command,
    session_label,
)
from wren.memory import Memory
from wren.sessions import SessionStore
from wren.messages import Completed, TextDelta
from wren.tools import Decision, PermissionRequest

try:
    # On macOS and Linux, importing readline quietly upgrades input(): arrow
    # keys, line editing and history (press Up for your last message).
    import readline  # noqa: F401
except ImportError:  # Windows has no readline; input() still works.
    pass


# --- A little colour ------------------------------------------------------
# ANSI escape codes: "\033[2m" turns on dim text, "\033[0m" resets. We only
# use them when writing to a real terminal, and respect the NO_COLOR convention.

_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _style(code: str) -> Callable[[str], str]:
    return lambda s: f"\033[{code}m{s}\033[0m" if _COLOR else s


dim, bold, red, green, cyan, yellow = _style("2"), _style("1"), _style("31"), _style("32"), _style("36"), _style("33")

# Inside an input() prompt, escape codes must be wrapped in \001...\002 so
# readline knows they take up no space on screen. Without that, long lines
# and history recall leave the cursor in the wrong place.
PROMPT = "\001\033[1;36m\002›\001\033[0m\002 " if _COLOR else "> "

# A permission prompt shows at most this much of a diff or a new file. Enough
# to judge most changes; a huge one says how much more there is.
MAX_DETAIL_LINES = 40

KEYS_HELP = """\
Wren asks before it edits a file or runs a command: y, n, or a for "always".
Ctrl-C while Wren is working stops it. Whatever it had already done stays done,
and stays in the conversation. Ctrl-D quits."""


class Repl:
    def __init__(
        self,
        agent: Agent,
        out: TextIO = sys.stdout,
        memory: Memory | None = None,
        sessions: SessionStore | None = None,
        notice: str = "",
        choose_session: bool = False,
    ) -> None:
        self.agent = agent
        self.memory = memory
        self.sessions = sessions
        self.notice = notice  # something to say at startup, e.g. which session was resumed
        self.choose_session = choose_session  # started with --resume: begin by listing saved sessions
        self.out = out
        self._mid_line = False  # True when streamed text didn't end with a newline

    def say(self, text: str = "", end: str = "\n") -> None:
        print(text, end=end, file=self.out, flush=True)

    def run(self) -> None:
        provider, tools = self.agent.provider, self.agent.tools
        effort = f" ({provider.effort} effort)" if provider.effort else ""
        self.say(f"{bold('wren')} {__version__} {dim(f'· {provider.name} · {provider.model}{effort} · {len(tools)} tools')}")
        self.say(dim("Type a message, /help for commands, Ctrl-D to quit."))
        if self.memory and self.memory.files:
            # Said up front, every time: these files steer the model, and in
            # a project you've just cloned you didn't write them.
            names = ", ".join(self.memory.shown(file.path) for file in self.memory.files)
            self.say(dim(f"Instructions loaded from: {names}  (/memory for details)"))
        if self.notice:
            self.say(dim(self.notice))
        if self.choose_session:
            self.list_sessions()

        while True:
            self.say()
            try:
                line = input(PROMPT).strip()
            except EOFError:  # Ctrl-D
                self.say()
                return
            except KeyboardInterrupt:  # Ctrl-C at the prompt: clear the line
                self.say()
                continue

            if not line:
                continue
            if line.startswith("/"):
                if not self.command(line):
                    return
                continue
            self.turn(line)

    # --- One conversational turn ------------------------------------------

    def turn(self, text: str) -> None:
        self._mid_line = False
        try:
            with closing(self.agent.run_turn(text)) as events:
                for event in events:
                    self.show(event)
        except KeyboardInterrupt:
            self._end_line()
            self.say(dim("[interrupted: anything Wren had already done is kept, and it knows where it stopped]"))
        except Exception as e:  # network trouble, bad API key, etc.
            self._end_line()
            self.say(red(f"error: {e}"))
            self.say(dim("The turn stopped there. What happened before the error is kept in the conversation."))

    def show(self, event: Any) -> None:
        if isinstance(event, TextDelta):
            self.say(event.text, end="")
            self._mid_line = not event.text.endswith("\n")

        elif isinstance(event, Completed):
            self._end_line()
            if event.stop_reason == "max_tokens":
                self.say(red("[reply cut off: hit the max_tokens limit]"))

        elif isinstance(event, ToolStarted):
            # Printed without a newline, so the result can finish the line
            # (unless a permission prompt comes in between).
            self.say(cyan("  ▸ ") + dim(format_call(event.call)), end="")
            self._mid_line = True

        elif isinstance(event, ToolFinished):
            result = event.result
            self._mid_line = False
            if result.is_error:
                self.say(red(f"  ✗ {result.summary}"))
            else:
                self.say(dim(f"  → {result.summary}"))

        elif isinstance(event, TurnFinished):
            self._end_line()
            if event.hit_step_limit:
                self.say(red(f"[stopped after {event.steps} model calls, the limit for one turn: say 'continue' to carry on]"))
            u = event.usage
            cached = f" ({u.cache_read_tokens:,} cached)" if u.cache_read_tokens else ""
            calls = f" · {event.steps} model calls" if event.steps > 1 else ""
            self.say(dim(f"↳ {u.sent_tokens:,} in{cached} · {u.output_tokens:,} out{calls}"))

        elif isinstance(event, CompactionStarted):
            self._end_line()
            why = "it passed the context budget" if event.automatic else "as you asked"
            self.say(dim(f"[compacting the conversation, {why}…]"))

        elif isinstance(event, Compacted):
            note = compaction_note(event)
            self.say(red(note) if event.error else dim(note))

    def _end_line(self) -> None:
        if self._mid_line:
            self.say()
            self._mid_line = False

    # --- Permission prompts ---------------------------------------------------

    def ask_permission(self, request: PermissionRequest) -> Decision:
        """Show what a tool wants to do and read the user's answer.

        This is called from deep inside a turn (agent -> registry ->
        permissions -> here), while the "▸ tool(...)" line is still open.
        """
        self._end_line()
        self.say(yellow(f"    {request.title}"))
        lines = request.detail.splitlines()
        for line in lines[:MAX_DETAIL_LINES]:
            self.say("    " + dim("│ ") + _colour_diff_line(line))
        if len(lines) > MAX_DETAIL_LINES:
            self.say("    " + dim(f"│ … and {len(lines) - MAX_DETAIL_LINES:,} more lines"))

        choices = "[y]es / [n]o"
        if request.scope is not None:
            choices += f" / [a]lways allow {request.scope_label} this session"
        while True:
            try:
                answer = input(f"    Allow? {choices}: ").strip().lower()
            except EOFError:  # Ctrl-D, or no more input: the safe answer is no
                self.say()
                return Decision.DENY
            # (Ctrl-C isn't caught here: it stops the whole turn, as it does anywhere else.)
            if answer in ("y", "yes"):
                return Decision.ALLOW
            if answer in ("n", "no"):
                return Decision.DENY
            if answer in ("a", "always") and request.scope is not None:
                return Decision.ALWAYS
            self.say(dim("    Please answer " + ("y, n or a." if request.scope is not None else "y or n.")))

    # --- Slash commands -----------------------------------------------------

    def command(self, line: str) -> bool:
        """Run a slash command. Returns False when the REPL should exit."""
        result = run_command(self.agent, line, KEYS_HELP, self.memory, self.sessions)
        if result.pick_provider:
            for label, _ in provider_choices(self.agent):
                self.say(f"  {label}")
            self.say(dim("Switch with /provider NAME"))
        if result.pick_model:
            self.list_models()
        if result.pick_effort:
            self.list_efforts()
        if result.pick_session:
            self.list_sessions()
        if result.compact:
            try:
                for event in self.agent.compact():
                    self.show(event)
            except KeyboardInterrupt:
                self.say(dim("[interrupted: the conversation is unchanged]"))
        if result.text:
            self.say(red(result.text) if result.is_error else result.text)
        return not result.quit


    # There's no picker in plain mode: list the choices, and say how to pick one.

    def list_models(self) -> None:
        models, problem = model_list(self.agent)
        if problem:
            self.say(red(problem))
            return
        current = self.agent.provider.model
        for model in models:
            self.say(f"  {model}" + (dim("  (in use)") if model == current else ""))
        if current not in models:
            self.say(f"  {current}" + dim("  (in use)"))
        self.say(dim("Switch with /model NAME"))

    def list_efforts(self) -> None:
        levels, problem = effort_list(self.agent)
        if problem:
            self.say(problem)
            return
        current = self.agent.provider.effort
        usual = default_effort(self.agent)
        for level in ["default", *levels]:
            in_use = level == current or (level == "default" and current is None)
            means = f" (which is {usual} for this model)" if level == "default" and usual else ""
            self.say(f"  {level}{means}" + (dim("  (in use)") if in_use else ""))
        self.say(dim("Switch with /effort LEVEL"))

    def list_sessions(self) -> None:
        saved = [s for s in self.sessions.saved() if s.id != self.sessions.current_id] if self.sessions else []
        if not saved:
            self.say(dim("There are no other saved conversations for this folder."))
            return
        for session in saved[:20]:
            self.say("  " + session_label(session))
        self.say(dim("Resume one with /resume ID (the first few characters of the id will do)"))


def _colour_diff_line(line: str) -> str:
    """Green for added lines, red for removed, as `git diff` does."""
    if line.startswith("+") and not line.startswith("+++"):
        return green(line)
    if line.startswith("-") and not line.startswith("---"):
        return red(line)
    return line
