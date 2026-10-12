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
import os
import textwrap
from dataclasses import dataclass
from datetime import datetime

from wren.agent import Agent, Compacted
from wren.config import API_KEYS, default_choice
from wren.memory import Memory
from wren.settings import UserDefaults
from wren.sessions import SavedSession, SessionError, SessionStore
from wren.messages import Message, ReasoningBlock, TextBlock, ToolResultBlock, ToolUseBlock
from wren.providers import PROVIDERS, make_provider
from wren.tools import ToolError

COMMANDS = """\
/help          show this list
/tools         list the tools Wren can use
/tools NAME    show exactly what the model is told about one tool
/history       show the conversation exactly as Wren holds it
/system        show the system prompt sent with every request
/usage         tokens used in this conversation, and how full the context is
/compact       replace the conversation with a summary of it, to save tokens
/provider      choose a different provider: anthropic, openai (/provider NAME)
/model         choose a different model (/model NAME switches straight to it)
/effort        choose how hard the model works on each reply (/effort LEVEL)
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
    # Shown for reference, not part of the conversation: a front end with
    # somewhere better than the transcript to put it should use that.
    panel: bool = False
    pick_provider: bool = False  # the front end should offer the providers to choose from
    pick_model: bool = False  # ...or the provider's models
    pick_effort: bool = False  # ...or the effort levels the current model takes
    pick_session: bool = False  # ...or this folder's saved sessions
    resumed: bool = False  # the conversation was swapped for a saved one; redraw it
    cleared: bool = False  # the conversation was reset; a front end with a transcript should empty it


def run_command(
    agent: Agent,
    line: str,
    keys_help: str = "",
    memory: Memory | None = None,
    sessions: SessionStore | None = None,
    defaults: UserDefaults | None = None,
) -> CommandResult:
    """Run one slash command. `keys_help` is the front end's own note about
    its keys (Ctrl-C, Escape...), added to the end of /help. `defaults` is
    where the user's saved defaults are read from (never written here: only
    a picker's Enter saves one)."""
    name, *args = line.split()
    name = name.lower()

    if name == "/provider":
        if not args:
            return CommandResult(pick_provider=True)
        text, ok = switch_provider(agent, args[0].lower(), user=defaults)
        return CommandResult(text, is_error=not ok)
    if name == "/model":
        if not args:
            return CommandResult(pick_model=True)
        return CommandResult(switch_model(agent, args[0]))
    if name == "/effort":
        if not args:
            return CommandResult(pick_effort=True)
        return CommandResult(switch_effort(agent, None if args[0].lower() == "default" else args[0].lower()))
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
        return CommandResult(memory.describe() if memory else "Memory files are switched off.", panel=True)
    if name == "/remember":
        return _remember(memory, line.partition(" ")[2].strip())

    if name in ("/quit", "/exit"):
        return CommandResult(quit=True)
    if name == "/help":
        return CommandResult(f"{COMMANDS}\n\n{keys_help}".rstrip(), panel=True)
    if name == "/clear":
        agent.reset()
        return CommandResult("Conversation cleared.", cleared=True)
    if name == "/compact":
        return CommandResult(compact=True)
    if name == "/usage":
        return CommandResult(_usage(agent), panel=True)
    if name == "/system":
        return CommandResult(agent.system_prompt, panel=True)
    if name == "/history":
        return CommandResult(_history(agent), panel=True)
    if name == "/permissions":
        return CommandResult(_permissions(agent), panel=True)
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
        return CommandResult("\n".join(lines), panel=True)
    tool = agent.tools.get(name)
    if tool is None:
        return CommandResult(f"no tool named {name}", is_error=True)
    spec = tool.spec()
    return CommandResult(
        json.dumps({"name": spec.name, "description": spec.description, "input_schema": spec.input_schema}, indent=2),
        panel=True,
    )


# --- Models and sessions: used by the commands above, and by the pickers ------


def missing_key(provider_name: str) -> str | None:
    """The environment variable a provider needs and doesn't have, if any."""
    key = API_KEYS.get(provider_name)
    return key[0] if key is not None and not os.environ.get(key[0]) else None


def provider_choices(agent: Agent, user: UserDefaults | None = None) -> list[tuple[str, str]]:
    """Each provider, with a note on where it stands: (label, name)."""
    rows = []
    for name in PROVIDERS:
        if name == agent.provider.name:
            note = f"   (in use, with {agent.provider.model})"
        elif missing_key(name):
            note = f"   (needs {missing_key(name)} to be set)"
        else:
            note = f"   ({agent.model_last_used_with(name) or default_choice(name, user)[0]})"
        rows.append((name + note, name))
    return rows


def switch_provider(
    agent: Agent,
    name: str,
    model: str | None = None,
    effort: str | None = None,
    user: UserDefaults | None = None,
) -> tuple[str, bool]:
    """Move the conversation to another provider. Returns what to say, and
    whether it worked. On any failure the current provider is kept.

    With no `model` given, it goes back to the one this conversation last
    used there, or else to the provider's default. When that is the default
    model, it also gets the default effort, as it would at startup. A model
    named by the caller (resuming a session) brings its own effort."""
    if name not in PROVIDERS:
        return f"no provider called {name}. There are: {', '.join(PROVIDERS)}", False
    if name == agent.provider.name:
        return f"Already using {name}.", True
    variable = missing_key(name)
    if variable:
        return f"{name} needs an API key: set {variable} in your shell and start Wren again.", False
    default_model, default_level = default_choice(name, user)
    if model is None:
        model = agent.model_last_used_with(name) or default_model
        if model == default_model:
            effort = default_level
    try:
        provider = make_provider(name, model, effort)
    except Exception as e:  # the SDK refused to start, for whatever reason
        return f"couldn't start {name}: {e}", False
    agent.set_provider(provider)
    note = f"Now using {name}, with {provider.model}."
    if agent.messages:
        note += " Its first reply re-reads the conversation at full price, and the earlier model's private reasoning doesn't carry over."
    return note, True


def remember_model(agent: Agent, user: UserDefaults | None) -> str:
    """Save the model now in use as this provider's default, for every
    future session. Called after the model was chosen with Enter.

    What's saved is the model with the effort it now has. Choosing a new
    model puts effort back to that model's own default (set_model), so that
    is what's saved with it; choosing the model already in use leaves its
    effort alone. Starting Wren later is not choosing a model, so it takes
    the pair as saved."""
    if user is None:
        return ""
    provider = agent.provider
    try:
        user.save(provider.name, provider.model, provider.effort)
    except OSError as e:
        return f"Couldn't save {provider.model} as your default: {e}"
    return f"Saved {provider.model} as your default {provider.name} model, for every session."


def remember_effort(agent: Agent, user: UserDefaults | None) -> str:
    """Save the effort now in use as the default effort for this provider's
    default model. Called after it was chosen with Enter.

    An effort level belongs to a model, and a model chosen only for this
    session (Space) isn't the default one, so there's nothing to attach
    the level to: it stays in this session."""
    if user is None:
        return ""
    provider = agent.provider
    default_model, _ = default_choice(provider.name, user)
    if provider.model != default_model:
        return (
            f"This effort is for this session only: your default {provider.name} model is {default_model}, "
            f"and efforts are saved with it. Choose {provider.model} with Enter to make it the default."
        )
    try:
        user.save(provider.name, provider.model, provider.effort)
    except OSError as e:
        return f"Couldn't save your default effort: {e}"
    level = provider.effort or "the model's own default"
    return f"Saved {level} as your default effort with {provider.model}, for every session."


def switch_model(agent: Agent, model: str) -> str:
    if model == agent.provider.model:
        return f"Already using {model}."
    had_effort = agent.provider.effort
    agent.set_model(model)
    note = f"Now using {model}."
    if had_effort:
        note += " Effort is back to this model's default (/effort to change it)."
    if agent.messages:
        note += " Its first reply re-reads the conversation at full price: the cache is kept per model."
    return note


def switch_effort(agent: Agent, effort: str | None) -> str:
    if effort == agent.provider.effort:
        return f"Effort is already {effort_name(agent)}."
    agent.set_effort(effort)
    note = f"Effort is now {effort_name(agent)}."
    if agent.messages:
        note += " The next reply re-reads the conversation at full price: changing effort resets the cache."
    return note


def effort_name(agent: Agent, explain: bool = False) -> str:
    """The effort in use, in words. With `explain`, a default is spelled
    out: what it is for this model, and how we know."""
    provider = agent.provider
    if provider.effort:
        return provider.effort
    if not explain:
        return "the model's default"
    default = default_effort(agent)
    if default is None:
        return f"the model's default (Wren doesn't know what that is for {provider.model})"
    source = getattr(provider, "default_effort_source", "")
    return f"the model's default, which is {default} for {provider.model}" + (f"  ({source})" if source else "")


def default_effort(agent: Agent) -> str | None:
    try:
        return agent.provider.default_effort()
    except Exception:
        return None


def _effort_evidence(agent: Agent) -> list[str]:
    """Keep the request record, response metadata and token counts separate.

    OpenAI can return effort in the response; other providers may not.
    Missing response metadata stays unknown, even when effort was sent.
    """
    call = agent.last_call
    name = agent.provider.name
    if call is None:
        return ["Last request: (none yet)"]
    sent = f"effort {call.effort_sent}" if call.effort_sent else "no effort setting, so the model's default applied"
    reported = (
        f"{call.effort_reported}  (as reported by {name})"
        if call.effort_reported is not None
        else f"not reported by {name}"
    )
    return [
        f"Last request was sent with: {sent}  (Wren's record)",
        f"Effort in the last reply: {reported}",
        f"Thinking in the last reply: {call.usage.thinking_tokens:,} of {call.usage.output_tokens:,} output tokens"
        f"  (as reported by {name}; more effort usually means more)",
    ]


def effort_list(agent: Agent) -> tuple[list[str], str]:
    """The effort levels the current model takes, or a note to show if it
    takes none, or they couldn't be fetched."""
    try:
        levels = agent.provider.efforts()
    except Exception as e:  # no network, a bad key, a model the API doesn't know...
        return [], f"couldn't find out which effort levels {agent.provider.model} takes: {e}. You can still set one: /effort LEVEL"
    if not levels:
        return [], (
            f"{agent.provider.model} has no separate effort setting. "
            "If this provider offers a choice, it's among its models: /model"
        )
    return levels, ""


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
    effort = f" at {session.effort} effort" if session.effort else ""
    if session.provider == provider.name and session.model:
        # Carry on with the model and effort it was using.
        if (session.model, session.effort) != (provider.model, provider.effort):
            provider.model, provider.effort = session.model, session.effort
            sessions.provider, sessions.model, sessions.effort = provider.name, session.model, session.effort
            note += f" Using {session.model}{effort}, as it was."
    elif session.model:
        # It was with a different provider. Go back to that one if we can.
        _, switched = switch_provider(agent, session.provider, session.model, session.effort)
        if switched:
            note += f" Back on {session.provider}, using {session.model}{effort}, as it was."
        else:
            note += f" It was using {session.provider}'s {session.model}, which isn't available now; continuing with {provider.model}."
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
        f"  of the input, {u.cache_read_tokens:,} were read from the cache (billed at a fraction of the price)",
        f"Conversation now: {context_meter(agent)}",
        "",
        f"Model asked for: {agent.provider.model}",
        # This line is the provider's word, not ours. If /model is really
        # working, it changes after the next reply.
        f"Last reply came from: {agent.answered_by or '(no reply yet)'}  (as reported by {agent.provider.name})",
        "",
        f"Effort asked for: {effort_name(agent, explain=True)}",
        *_effort_evidence(agent),
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


def plain_call(call: ToolUseBlock) -> tuple[str, str]:
    """A tool call as a person would say it: (headline, detail).

    "Read src/app.py" is easier to follow at a glance than
    read_file(path='src/app.py'), and for a shell command the difference is
    larger still: the headline is the model's own sentence about what the
    command is for, and the detail is the command. /history still shows
    every call exactly as it was made.
    """
    args = call.input if isinstance(call.input, dict) else {}
    path, pattern = args.get("path"), args.get("pattern")
    if call.name == "shell" and isinstance(args.get("command"), str):
        said = args.get("description")
        headline = shorten(" ".join(str(said).split()), 110) if said else "Run a command"
        return headline, "$ " + shorten(" ".join(args["command"].split()), 200)
    if call.name == "read_file" and path:
        start = args.get("offset")
        return f"Read {path}" + (f" from line {start}" if start else ""), ""
    if call.name == "list_files":
        return f"List {pattern or '*'}" + (f" in {path}" if path else ""), ""
    if call.name == "search" and pattern:
        return f"Search for {shorten(repr(pattern), 60)}" + (f" in {path}" if path else ""), ""
    if call.name == "edit_file" and path:
        return f"Edit {path}", ""
    if call.name == "write_file" and path:
        return f"Write {path}", ""
    return format_call(call), ""  # a tool we have no words for: show the call itself


def format_call(call: ToolUseBlock) -> str:
    """A tool call written like a Python call: read_file(path='app.py')."""
    args = ", ".join(f"{key}={shorten(repr(value), 60)}" for key, value in call.input.items())
    return f"{call.name}({args})"


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
