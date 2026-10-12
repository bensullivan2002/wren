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
from datetime import datetime

from wren.agent import Agent, Compacted
from wren.memory import Memory
from wren.sessions import SavedSession, SessionError, SessionStore
from wren.messages import Message, ReasoningBlock, TextBlock, ToolResultBlock, ToolUseBlock
from wren.tools import ToolError

COMMANDS = """\
/help          show this list
/tools         list the tools Wren can use
/tools NAME    show exactly what the model is told about one tool
/history       show the conversation exactly as Wren holds it
/system        show the system prompt sent with every request
/usage         tokens used in this conversation, and how full the context is
/compact       replace the conversation with a summary of it, to save tokens
/model         choose a different model (/model NAME switches straight to it)
/resume        go back to an earlier conversation in this folder (/resume ID)
/permissions   show what you've said Wren may always do this session
/memory        show which memory files (WREN.md, AGENTS.md) are loaded
/remember X    add the note X to the project's memory file
/clear         start a fresh conversation (the old one stays saved)
/quit          exit"""


@dataclass
class CommandResult:
    text: str = ""  # what to show
    is_error: bool = False
    quit: bool = False  # the front end should exit
    compact: bool = False  # the front end should run agent.compact() (a model call, so it takes a while)
    pick_model: bool = False  # the front end should offer the provider's models to choose from
    pick_session: bool = False  # ...or this folder's saved sessions
    resumed: bool = False  # the conversation was swapped for a saved one; redraw it
    cleared: bool = False  # the conversation was reset; a front end with a transcript should empty it


def run_command(
    agent: Agent,
    line: str,
    keys_help: str = "",
    memory: Memory | None = None,
    sessions: SessionStore | None = None,
) -> CommandResult:
    """Run one slash command. `keys_help` is the front end's own note about
    its keys (Ctrl-C, Escape...), added to the end of /help."""
    name, *args = line.split()
    name = name.lower()

    if name == "/model":
        if not args:
            return CommandResult(pick_model=True)
        return CommandResult(switch_model(agent, args[0]))
    if name == "/resume":
        if sessions is None:
            return CommandResult("Sessions aren't being saved.", is_error=True)
        if not args:
            return CommandResult(pick_session=True)
        try:
            return CommandResult(resume_session(agent, sessions, sessions.find(args[0])), resumed=True)
        except (SessionError, ValueError) as e:
            return CommandResult(f"couldn't resume: {e}", is_error=True)

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


# --- Models and sessions: used by the commands above, and by the pickers ------


def switch_model(agent: Agent, model: str) -> str:
    if model == agent.provider.model:
        return f"Already using {model}."
    agent.set_model(model)
    note = f"Now using {model}."
    if agent.messages:
        note += " Its first reply re-reads the conversation at full price: the cache is kept per model."
    return note


def model_list(agent: Agent) -> tuple[list[str], str]:
    """The provider's models, and a problem to report if they couldn't be fetched."""
    try:
        return agent.provider.models(), ""
    except Exception as e:  # no network, a bad key...
        return [], f"couldn't list the models: {e}. You can still switch by name: /model NAME"


def resume_session(agent: Agent, sessions: SessionStore, session: SavedSession) -> str:
    """Swap the conversation for a saved one, and carry on writing to its file."""
    if session.id == sessions.current_id:
        raise SessionError("that's the conversation you're in")
    # Point the journal at that session's file first, so that anything
    # `restore` adds to tidy up an unfinished turn is written there too.
    previous = sessions.path
    sessions.continue_in(session)
    try:
        # Raises ValueError, keeping the old conversation, if it's unusable.
        agent.restore(session.messages, session.usage, session.context_tokens)
    except ValueError:
        sessions.path = previous
        raise

    provider = agent.provider
    note = f"Resumed {session.id} ({ago(session.modified)}): {len(agent.messages)} messages."
    if session.model and session.model != provider.model:
        if session.provider == provider.name:
            provider.model = session.model  # carry on with the model it was using
            sessions.provider, sessions.model = provider.name, session.model
            note += f" Using {session.model}, as it was."
        else:
            note += f" It was using {session.provider}'s {session.model}; continuing with {provider.model}."
    return note + " Files may have changed since."


def session_label(session: SavedSession) -> str:
    # What it was about comes first: that's what you recognise it by.
    return f"{ago(session.modified):<14}  {session.title}   [{session.id}]"


def ago(when: datetime, now: datetime | None = None) -> str:
    seconds = int(((now or datetime.now()) - when).total_seconds())
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "just now"


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
        f"This conversation: {u.sent_tokens:,} tokens in · {u.output_tokens:,} out",
        f"  of the input, {u.cache_read_tokens:,} were read from the cache (about a tenth of the price)",
        f"Conversation now: {context_meter(agent)}",
        f"Model asked for: {agent.provider.model}",
        # The second line is the provider's word, not ours. If /model is
        # really working, it changes after the next reply.
        f"Last reply came from: {agent.answered_by or '(no reply yet)'}  (as reported by {agent.provider.name})",
    ]
    return "\n".join(lines)


def context_meter(agent: Agent) -> str:
    """How big the conversation is, against the budget: "12,400 tokens, 8% of 150,000"."""
    size = f"{agent.context_tokens:,} tokens"
    if not agent.context_budget:
        return size
    return f"{size}, {budget_used(agent)} of the {agent.context_budget:,} budget"


def budget_used(agent: Agent) -> str:
    """How much of the context budget is used: "12%". A conversation that
    exists but rounds to nothing shows "<1%", because "0%" reads as "empty"
    or "not working"."""
    percent = round(100 * agent.context_tokens / agent.context_budget)
    return "<1%" if percent == 0 and agent.context_tokens else f"{percent}%"


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
