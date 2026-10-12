"""Slash commands, and the bits of text every front end shows the same way.

Wren has two front ends: the full-screen app (tui.py) and the plain REPL
(repl.py). `/tools` should say the same thing in both, so the commands live
here and return plain text. Each front end decides how to display it, and
what to do when a command asks to quit or clears the conversation.

Nothing here prints or reads input. That's the same rule the agent follows,
for the same reason: it's what lets more than one interface exist.
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass

from wren.agent import Agent, Compacted
from wren.memory import Memory
from wren.messages import Message, ReasoningBlock, TextBlock, ToolResultBlock, ToolUseBlock
from wren.tools import ToolError

COMMANDS = """\
/help          show this list
/tools         list the tools Wren can use
/tools NAME    show exactly what the model is told about one tool
/history       show the conversation exactly as Wren holds it
/system        show the system prompt sent with every request
/usage         tokens used this session, and how full the context is
/compact       replace the conversation with a summary of it, to save tokens
/permissions   show what you've said Wren may always do this session
/memory        show which memory files (WREN.md, AGENTS.md) are loaded
/remember X    add the note X to the project's memory file
/clear         start a fresh conversation
/quit          exit"""


@dataclass
class CommandResult:
    text: str = ""  # what to show
    is_error: bool = False
    quit: bool = False  # the front end should exit
    compact: bool = False  # the front end should run agent.compact() (a model call, so it takes a while)
    cleared: bool = False  # the conversation was reset; a front end with a transcript should empty it


def run_command(agent: Agent, line: str, keys_help: str = "", memory: Memory | None = None) -> CommandResult:
    """Run one slash command. `keys_help` is the front end's own note about
    its keys (Ctrl-C, Escape...), added to the end of /help."""
    name, *args = line.split()
    name = name.lower()

    if name == "/memory":
        return CommandResult(memory.describe() if memory else "Memory files are switched off.")
    if name == "/remember":
        return _remember(memory, line.partition(" ")[2].strip())

    if name in ("/quit", "/exit"):
        return CommandResult(quit=True)
    if name == "/help":
        return CommandResult(f"{COMMANDS}\n\n{keys_help}".rstrip())
    if name == "/clear":
        agent.reset()
        return CommandResult("Conversation cleared.", cleared=True)
    if name == "/compact":
        return CommandResult(compact=True)
    if name == "/usage":
        return CommandResult(_usage(agent))
    if name == "/system":
        return CommandResult(agent.system_prompt)
    if name == "/history":
        return CommandResult(_history(agent))
    if name == "/permissions":
        return CommandResult(_permissions(agent))
    if name == "/tools":
        return _tools(agent, args[0] if args else None)
    return CommandResult(f"unknown command {name}  (try /help)", is_error=True)


def _tools(agent: Agent, name: str | None) -> CommandResult:
    if name is None:
        lines = []
        for tool in agent.tools:
            first_sentence = tool.description.split(". ")[0].rstrip(".")
            lines.append(f"{tool.name:<12} {first_sentence}")
        lines.append("\n/tools NAME shows exactly what the model is told about a tool.")
        return CommandResult("\n".join(lines))
    tool = agent.tools.get(name)
    if tool is None:
        return CommandResult(f"no tool named {name}", is_error=True)
    spec = tool.spec()
    return CommandResult(
        json.dumps({"name": spec.name, "description": spec.description, "input_schema": spec.input_schema}, indent=2)
    )


def _remember(memory: Memory | None, note: str) -> CommandResult:
    if memory is None:
        return CommandResult("Memory files are switched off.", is_error=True)
    if not note:
        return CommandResult("Say what to remember: /remember run the tests with make check", is_error=True)
    try:
        path = memory.remember(note)
    except (OSError, ToolError) as e:
        return CommandResult(f"couldn't save that: {e}", is_error=True)
    return CommandResult(
        f"Added to {memory.shown(path)}. It's on disk now, and Wren will see it from the next "
        "conversation on (/clear, or restart)."
    )


def _usage(agent: Agent) -> str:
    u = agent.usage
    lines = [
        f"This session: {u.sent_tokens:,} tokens in · {u.output_tokens:,} out",
        f"  of the input, {u.cache_read_tokens:,} were read from the cache (about a tenth of the price)",
        f"Conversation now: {context_meter(agent)}",
    ]
    return "\n".join(lines)


def context_meter(agent: Agent) -> str:
    """How big the conversation is, against the budget: "12,400 tokens, 8% of 150,000"."""
    size = f"{agent.context_tokens:,} tokens"
    if not agent.context_budget:
        return size
    percent = round(100 * agent.context_tokens / agent.context_budget)
    return f"{size}, {percent}% of the {agent.context_budget:,} budget"


def compaction_note(event: Compacted) -> str:
    if event.error:
        return f"[couldn't compact the conversation: {event.error}. It is unchanged.]"
    return (
        f"[conversation compacted: about {event.before_tokens:,} → {event.after_tokens:,} tokens. "
        "Wren now works from its summary; /history shows it.]"
    )


def _permissions(agent: Agent) -> str:
    always = agent.tools.permissions.always
    if not always:
        return "Nothing is always allowed: Wren asks before every edit and command."
    return "Allowed without asking, until you quit:\n" + "\n".join(f"  {label}" for label in always.values())


def _history(agent: Agent) -> str:
    if not agent.messages:
        return "(empty: nothing said yet)"
    parts = []
    for i, message in enumerate(agent.messages, 1):
        parts.append(f"[{i}] {message.role}")
        parts.append(textwrap.indent(_describe(message), "    "))
    return "\n".join(parts)


def _describe(message: Message) -> str:
    parts = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ToolUseBlock):
            parts.append(f"<tool_use id={block.id} {block.name} {json.dumps(block.input)}>")
        elif isinstance(block, ToolResultBlock):
            error = " error" if block.is_error else ""
            parts.append(f"<tool_result for={block.tool_use_id}{error}> {shorten(block.content, 300)!r}")
        elif isinstance(block, ReasoningBlock):
            parts.append("<reasoning: kept to send back to the model, not readable>")
    return "\n".join(parts)


# --- Text both front ends show ------------------------------------------------


def format_call(call: ToolUseBlock) -> str:
    """A tool call written like a Python call: read_file(path='app.py')."""
    args = ", ".join(f"{key}={shorten(repr(value), 60)}" for key, value in call.input.items())
    return f"{call.name}({args})"


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
