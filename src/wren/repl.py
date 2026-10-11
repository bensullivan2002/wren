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
from typing import Callable, TextIO

from wren import __version__
from wren.agent import Agent
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


dim, bold, red = _style("2"), _style("1"), _style("31")

# Inside an input() prompt, escape codes must be wrapped in \001...\002 so
# readline knows they take up no space on screen. Without that, long lines
# and history recall leave the cursor in the wrong place.
PROMPT = "\001\033[1;36m\002›\001\033[0m\002 " if _COLOR else "> "

HELP = """\
/help      show this list
/history   show the conversation exactly as Wren holds it
/system    show the system prompt sent with every request
/usage     tokens used this session
/clear     start a fresh conversation
/quit      exit (Ctrl-D works too)

Ctrl-C while Wren is replying stops the reply and discards that turn."""


class Repl:
    def __init__(self, agent: Agent, out: TextIO = sys.stdout) -> None:
        self.agent = agent
        self.out = out

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def run(self) -> None:
        provider = self.agent.provider
        self.say(f"{bold('wren')} {__version__} {dim(f'· {provider.name} · {provider.model}')}")
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
        try:
            with closing(self.agent.run_turn(text)) as events:
                for event in events:
                    if isinstance(event, TextDelta):
                        # flush=True so each fragment appears immediately,
                        # instead of waiting in Python's output buffer.
                        print(event.text, end="", file=self.out, flush=True)
                    elif isinstance(event, Completed):
                        self.say()
                        if event.stop_reason == "max_tokens":
                            self.say(red("[reply cut off: hit the max_tokens limit]"))
                        u = event.usage
                        self.say(dim(f"↳ {u.input_tokens:,} in · {u.output_tokens:,} out"))
        except KeyboardInterrupt:
            self.say()
            self.say(dim("[interrupted, that turn was discarded]"))
        except Exception as e:  # network trouble, bad API key, etc.
            self.say()
            self.say(red(f"error: {e}"))
            self.say(dim("That turn was discarded; the conversation is unchanged."))

    # --- Slash commands -----------------------------------------------------

    def command(self, line: str) -> bool:
        """Run a slash command. Returns False when the REPL should exit."""
        name = line.split()[0].lower()
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
        else:
            self.say(red(f"unknown command {name}") + dim("  (try /help)"))
        return True

    def show_history(self) -> None:
        if not self.agent.messages:
            self.say(dim("(empty: nothing said yet)"))
            return
        for i, message in enumerate(self.agent.messages, 1):
            self.say(bold(f"[{i}] {message.role}"))
            self.say(textwrap.indent(_describe(message), "    "))


def _describe(message: Message) -> str:
    parts = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ToolUseBlock):
            parts.append(f"<tool_use {block.name} {json.dumps(block.input)}>")
        elif isinstance(block, ToolResultBlock):
            parts.append(f"<tool_result {'error ' if block.is_error else ''}{block.content!r}>")
    return "\n".join(parts)
