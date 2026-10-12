"""The terminal front end: a REPL (Read, Eval, Print, Loop).

    read a line -> run a turn on the agent -> print events as they stream -> repeat

Everything about *showing* things lives here, and nothing about *doing*
things. The agent hands us events; we decide how they look. Swapping this
file for a fancier interface later shouldn't require touching the agent.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from contextlib import closing
from typing import Any, Callable, TextIO

from wren import __version__
from wren.agent import Agent, ToolFinished, ToolStarted, TurnFinished
from wren.messages import Completed, Message, TextBlock, TextDelta, ToolResultBlock, ToolUseBlock

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


dim, bold, red, cyan = _style("2"), _style("1"), _style("31"), _style("36")

# Inside an input() prompt, escape codes must be wrapped in \001...\002 so
# readline knows they take up no space on screen. Without that, long lines
# and history recall leave the cursor in the wrong place.
PROMPT = "\001\033[1;36m\002›\001\033[0m\002 " if _COLOR else "> "

HELP = """\
/help          show this list
/tools         list the tools Wren can use
/tools NAME    show exactly what the model is told about one tool
/history       show the conversation exactly as Wren holds it
/system        show the system prompt sent with every request
/usage         tokens used this session
/clear         start a fresh conversation
/quit          exit (Ctrl-D works too)

Ctrl-C while Wren is working stops it and discards that turn."""


class Repl:
    def __init__(self, agent: Agent, out: TextIO = sys.stdout) -> None:
        self.agent = agent
        self.out = out
        self._mid_line = False  # True when streamed text didn't end with a newline

    def say(self, text: str = "", end: str = "\n") -> None:
        print(text, end=end, file=self.out, flush=True)

    def run(self) -> None:
        provider, tools = self.agent.provider, self.agent.tools
        self.say(f"{bold('wren')} {__version__} {dim(f'· {provider.name} · {provider.model} · {len(tools)} tools')}")
        self.say(dim("Type a message, /help for commands, Ctrl-D to quit."))

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
            self.say(dim("[interrupted: that turn was discarded]"))
        except Exception as e:  # network trouble, bad API key, etc.
            self._end_line()
            self.say(red(f"error: {e}"))
            self.say(dim("That turn was discarded; the conversation is unchanged."))

    def show(self, event: Any) -> None:
        if isinstance(event, TextDelta):
            self.say(event.text, end="")
            self._mid_line = not event.text.endswith("\n")

        elif isinstance(event, Completed):
            self._end_line()
            if event.stop_reason == "max_tokens":
                self.say(red("[reply cut off: hit the max_tokens limit]"))

        elif isinstance(event, ToolStarted):
            # Printed without a newline, so the result can finish the line.
            self.say(cyan("  ▸ ") + dim(format_call(event.call)), end="")

        elif isinstance(event, ToolFinished):
            result = event.result
            if result.is_error:
                self.say(red(f"  ✗ {result.summary}"))
            else:
                self.say(dim(f"  → {result.summary}"))

        elif isinstance(event, TurnFinished):
            self._end_line()
            if event.hit_step_limit:
                self.say(red(f"[stopped after {event.steps} model calls, the limit for one turn: say 'continue' to carry on]"))
            u = event.usage
            calls = f" · {event.steps} model calls" if event.steps > 1 else ""
            self.say(dim(f"↳ {u.input_tokens:,} in · {u.output_tokens:,} out{calls}"))

    def _end_line(self) -> None:
        if self._mid_line:
            self.say()
            self._mid_line = False

    # --- Slash commands -----------------------------------------------------

    def command(self, line: str) -> bool:
        """Run a slash command. Returns False when the REPL should exit."""
        name, *args = line.split()
        name = name.lower()
        if name in ("/quit", "/exit"):
            return False
        if name == "/help":
            self.say(HELP)
        elif name == "/clear":
            self.agent.reset()
            self.say(dim("Conversation cleared."))
        elif name == "/usage":
            u = self.agent.usage
            self.say(f"{u.input_tokens:,} tokens in · {u.output_tokens:,} out (this session)")
        elif name == "/system":
            self.say(self.agent.system_prompt)
        elif name == "/history":
            self.show_history()
        elif name == "/tools":
            self.show_tools(args[0] if args else None)
        else:
            self.say(red(f"unknown command {name}") + dim("  (try /help)"))
        return True

    def show_tools(self, name: str | None) -> None:
        if name is None:
            for tool in self.agent.tools:
                first_sentence = tool.description.split(". ")[0].rstrip(".")
                self.say(f"{bold(tool.name):<24} {first_sentence}")
            self.say(dim("\n/tools NAME shows exactly what the model is told about a tool."))
            return
        tool = self.agent.tools.get(name)
        if tool is None:
            self.say(red(f"no tool named {name}"))
            return
        spec = tool.spec()
        self.say(json.dumps({"name": spec.name, "description": spec.description, "input_schema": spec.input_schema}, indent=2))

    def show_history(self) -> None:
        if not self.agent.messages:
            self.say(dim("(empty: nothing said yet)"))
            return
        for i, message in enumerate(self.agent.messages, 1):
            self.say(bold(f"[{i}] {message.role}"))
            self.say(textwrap.indent(_describe(message), "    "))


def format_call(call: ToolUseBlock) -> str:
    """A tool call written like a Python call: read_file(path='app.py')."""
    args = ", ".join(f"{key}={_shorten(repr(value), 60)}" for key, value in call.input.items())
    return f"{call.name}({args})"


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _describe(message: Message) -> str:
    parts = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ToolUseBlock):
            parts.append(f"<tool_use id={block.id} {block.name} {json.dumps(block.input)}>")
        elif isinstance(block, ToolResultBlock):
            error = " error" if block.is_error else ""
            parts.append(f"<tool_result for={block.tool_use_id}{error}> {_shorten(block.content, 300)!r}")
    return "\n".join(parts)
