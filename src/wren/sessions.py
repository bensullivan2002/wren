"""Sessions: conversations saved to disk as they happen, so they can be resumed.

Until now, quitting Wren threw the conversation away. A session is that
conversation written to a file, one line at a time, as it happens:

    ~/.wren/sessions/<project>/<id>.jsonl

Three choices shape this module.

1. JSON LINES, APPEND-ONLY. Each line of the file is one small JSON object.
   Saving a message means writing one more line on the end, and nothing
   already written is ever touched. So there's no "save" step to forget, and
   a crash, a power cut or `kill -9` can cost at most the line being written.
   A file rewritten in full on every change is easier to read back, and can
   be destroyed by being interrupted half-way.

   The file is a LOG of what happened, not a snapshot of the history. To get
   the history back, replay the log from the top (`load`). That's how a line
   can say "the history was replaced by these messages" (a compaction)
   without the earlier lines having to be rewritten.

2. OUTSIDE THE PROJECT. A transcript holds whatever the model read: source
   files, command output, sometimes secrets. Kept under your home directory,
   readable only by you, it can't be committed by a careless `git add .`.

3. THE AGENT DOESN'T KNOW ABOUT FILES. It is given a "journal": an object
   with four methods (see `Journal`). This module provides one that writes
   to disk; a test, or a front end with other ideas, can provide another.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from wren import __version__
from wren.messages import Block, Message, ReasoningBlock, TextBlock, ToolResultBlock, ToolUseBlock, Usage

FORMAT_VERSION = 1  # bump if the lines change shape in a way old code can't read


class Journal(Protocol):
    """What the agent tells whoever is recording the conversation."""

    def append(self, message: Message) -> None:
        """A message was added to the end of the history."""

    def replace(self, messages: list[Message]) -> None:
        """The whole history was replaced (a compaction)."""

    def model_changed(self, provider: str, model: str, effort: str | None) -> None:
        """The conversation moved to a different model, or effort level."""

    def spent(self, usage: Usage, context_tokens: int) -> None:
        """A model call finished: what it cost, and how big the conversation now is."""

    def reset(self) -> None:
        """The conversation was cleared. Whatever comes next is a new one."""


# --- Messages <-> JSON -----------------------------------------------------------
#
# Wren's message types are plain dataclasses, so this is nearly mechanical.
# It's written out by hand, not derived automatically, because a file format
# is a promise: these names are what old session files contain, and they
# must keep meaning the same thing even if the Python classes are renamed.


def block_to_json(block: Block) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        return {"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content, "is_error": block.is_error}
    if isinstance(block, ReasoningBlock):
        # Saved sealed, as it's held in memory: the provider's own data,
        # which only that provider can read (see messages.ReasoningBlock).
        return {"type": "reasoning", "provider": block.provider, "data": block.data}
    raise TypeError(f"unknown block type: {type(block).__name__}")


def block_from_json(data: dict[str, Any]) -> Block:
    kind = data.get("type")
    if kind == "text":
        return TextBlock(data["text"])
    if kind == "tool_use":
        return ToolUseBlock(data["id"], data["name"], data["input"])
    if kind == "tool_result":
        return ToolResultBlock(data["tool_use_id"], data["content"], data.get("is_error", False))
    if kind == "reasoning":
        return ReasoningBlock(data["provider"], data["data"])
    raise ValueError(f"unknown block type in a session file: {kind!r}")


def message_to_json(message: Message) -> dict[str, Any]:
    return {"role": message.role, "content": [block_to_json(block) for block in message.content]}


def message_from_json(data: dict[str, Any]) -> Message:
    return Message(data["role"], [block_from_json(block) for block in data["content"]])


# --- One saved session ---------------------------------------------------------------


@dataclass
class SavedSession:
    """A session as read back from disk."""

    id: str
    path: Path
    messages: list[Message]
    provider: str
    model: str
    effort: str | None  # None: the model's own default
    created: str  # ISO date and time
    title: str  # the first thing the user said, shortened
    usage: Usage  # what the conversation has cost so far, added up
    context_tokens: int | None  # its size at the last model call; None if the file doesn't say

    @property
    def modified(self) -> datetime:
        return datetime.fromtimestamp(self.path.stat().st_mtime)


class SessionError(Exception):
    """A session file couldn't be read."""


def load(path: Path) -> SavedSession:
    """Read a session file by replaying its lines from the top."""
    messages: list[Message] = []
    meta: dict[str, Any] = {}
    title = ""
    usage = Usage()
    context_tokens: int | None = None
    try:
        with path.open(encoding="utf-8") as file:
            for number, line in enumerate(file, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    kind = record.get("type")
                    if kind == "meta":
                        meta = record
                    elif kind == "message":
                        message = message_from_json(record)
                        messages.append(message)
                        if not title and message.role == "user" and message.text.strip():
                            title = message.text.strip()
                    elif kind == "replaced":
                        messages = [message_from_json(m) for m in record["messages"]]
                    elif kind == "spent":
                        usage += Usage(
                            record.get("input_tokens", 0),
                            record.get("output_tokens", 0),
                            record.get("cache_read_tokens", 0),
                            record.get("cache_write_tokens", 0),
                            record.get("thinking_tokens", 0),
                        )
                        context_tokens = record.get("context_tokens", context_tokens)
                    elif kind == "model":
                        meta.update(provider=record["provider"], model=record["model"], effort=record.get("effort"))
                    # Any other type is from a newer Wren: skip it, don't fail.
                except (json.JSONDecodeError, KeyError, ValueError, TypeError, AttributeError) as e:
                    # The very last line may be half-written if Wren was
                    # killed mid-write. Anywhere else, the file is damaged.
                    if _is_last_line(path, number):
                        break
                    raise SessionError(f"{path.name} is damaged at line {number}: {e}") from None
    except OSError as e:
        raise SessionError(f"couldn't read {path.name}: {e}") from None

    if meta.get("version", FORMAT_VERSION) > FORMAT_VERSION:
        raise SessionError(f"{path.name} was saved by a newer version of Wren, which this one can't read")
    first_line = (title.splitlines() or ["(no messages)"])[0]
    return SavedSession(
        id=path.stem,
        path=path,
        messages=messages,
        provider=meta.get("provider", ""),
        model=meta.get("model", ""),
        effort=meta.get("effort"),
        created=meta.get("created", ""),
        title=first_line if len(first_line) <= 70 else first_line[:69] + "…",
        usage=usage,
        context_tokens=context_tokens,
    )


def _is_last_line(path: Path, number: int) -> bool:
    with path.open(encoding="utf-8") as file:
        return sum(1 for _ in file) == number


# --- The store: where sessions live, and the journal that writes one --------------------


class SessionStore:
    """The saved sessions of one project, and the one being written now.

    It is the agent's `Journal`: the agent calls append/replace/..., and
    lines appear in the current session's file.
    """

    def __init__(
        self, cwd: Path, provider: str, model: str, home: Path | None = None, effort: str | None = None
    ) -> None:
        self.cwd = cwd.resolve()
        home = home if home is not None else Path.home()
        self.folder = home / ".wren" / "sessions" / project_folder_name(self.cwd)
        self.provider, self.model, self.effort = provider, model, effort
        # No file yet. It's created by the first message, so opening Wren
        # and quitting doesn't leave an empty session behind.
        self.path: Path | None = None

    # --- Writing: the Journal the agent talks to --------------------------------

    def append(self, message: Message) -> None:
        self._write({"type": "message", **message_to_json(message)})

    def replace(self, messages: list[Message]) -> None:
        self._write({"type": "replaced", "messages": [message_to_json(m) for m in messages]})

    def model_changed(self, provider: str, model: str, effort: str | None) -> None:
        self.provider, self.model, self.effort = provider, model, effort
        if self.path is not None:  # otherwise the first line will carry it
            self._write({"type": "model", "provider": provider, "model": model, "effort": effort})

    def spent(self, usage: Usage, context_tokens: int) -> None:
        # Saved so that a resumed conversation knows its totals and its size
        # straight away. They can't be worked out from the messages: only
        # the provider's own count is exact (see step 5).
        if self.path is not None:
            self._write(
                {
                    "type": "spent",
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_read_tokens": usage.cache_read_tokens,
                    "cache_write_tokens": usage.cache_write_tokens,
                    "thinking_tokens": usage.thinking_tokens,
                    "context_tokens": context_tokens,
                }
            )

    def reset(self) -> None:
        self.path = None  # the next message starts a new file

    def _write(self, record: dict[str, Any]) -> None:
        try:
            if self.path is None:
                self._start()
            # Opened, written and closed for every line. Slower than keeping
            # the file open, but each line is on disk before we move on,
            # and there's no open file to lose if Wren is killed.
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # A full disk or a read-only home shouldn't stop the conversation.
            # It just isn't saved.
            pass

    def _start(self) -> None:
        # 0o700 / 0o600: readable by you and nobody else on the machine.
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        now = datetime.now()
        # Ids sort by time, are readable, and the random tail makes two
        # sessions started in the same second distinct.
        self.path = self.folder / f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}.jsonl"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            meta = {
                "type": "meta",
                "version": FORMAT_VERSION,
                "wren": __version__,
                "cwd": str(self.cwd),
                "created": now.isoformat(timespec="seconds"),
                "provider": self.provider,
                "model": self.model,
                "effort": self.effort,
            }
            file.write(json.dumps(meta) + "\n")

    # --- Reading -------------------------------------------------------------------

    @property
    def current_id(self) -> str | None:
        return self.path.stem if self.path is not None else None

    def saved(self) -> list[SavedSession]:
        """This project's sessions, most recently used first. Files that
        can't be read are left out."""
        if not self.folder.is_dir():
            return []
        found = []
        for path in self.folder.glob("*.jsonl"):
            try:
                session = load(path)
            except SessionError:
                continue
            if session.messages:
                found.append(session)
        return sorted(found, key=lambda s: s.modified, reverse=True)

    def find(self, wanted: str | None) -> SavedSession:
        """The most recent session, or the one whose id starts with `wanted`."""
        sessions = self.saved()
        if not sessions:
            raise SessionError("there are no saved sessions for this folder yet")
        if not wanted:
            return sessions[0]
        matches = [s for s in sessions if s.id.startswith(wanted)]
        if len(matches) != 1:
            problem = "no session" if not matches else f"{len(matches)} sessions"
            raise SessionError(f"{problem} here with an id starting {wanted!r} (/resume lists them)")
        return matches[0]

    def continue_in(self, session: SavedSession) -> None:
        """Carry on writing to an existing session's file."""
        self.path = session.path


def project_folder_name(cwd: Path) -> str:
    """A folder name for a project's sessions, made from its path:
    /Users/ben/code/wren -> -Users-ben-code-wren.

    Readable, so you can find a project's sessions by eye. A very long path
    is cut short, with a hash of the whole thing added so that two long
    paths can't end up sharing a folder.
    """
    name = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    if len(name) > 100:
        name = name[:100] + "-" + hashlib.sha256(str(cwd).encode()).hexdigest()[:8]
    return name
