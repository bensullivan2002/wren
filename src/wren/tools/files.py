"""Helpers shared by the file tools: keeping paths inside the working
directory, walking folders, spotting binary files, matching globs, and
reading and writing text without disturbing it."""

from __future__ import annotations

import difflib
import os
import re
from pathlib import Path
from typing import Iterator

from wren.tools.base import ToolContext, ToolError
from wren.tools.sandbox import PROTECTED

# Folders that are almost never what the model is looking for, and can be
# enormous. Skipping them keeps listings and searches fast and relevant.
# (Reading .gitignore properly would be better; it's on the list for later.)
IGNORED_DIRS = frozenset(
    {
        ".git", ".hg", ".svn",
        "node_modules", ".venv", "venv", "__pycache__",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    }
)


def resolve(ctx: ToolContext, path: str) -> Path:
    """Turn a path from the model into an absolute path inside the working directory.

    Relative paths are relative to the working directory. `resolve()` collapses
    `..` and follows symlinks *before* the check, so neither can be used to
    escape: `../../etc/passwd`, `/etc/passwd`, and a symlink pointing at
    /etc/passwd all end up outside the root and are refused.
    """
    candidate = (ctx.root / path).resolve()  # if `path` is absolute, root / path == path
    if not candidate.is_relative_to(ctx.root):
        raise ToolError(
            f"'{path}' is outside the working directory ({ctx.root}). "
            "Wren's tools can only access files inside it."
        )
    return candidate


def resolve_for_writing(ctx: ToolContext, path: str) -> Path:
    """Like resolve(), for tools that are about to change the file.

    One extra rule: nothing inside `.git` may be written. That folder is
    git's own database, and its `hooks/` are scripts git runs by itself, so
    a file written there could run later without anyone approving it.
    """
    target = resolve(ctx, path)
    if ".git" in target.relative_to(ctx.root).parts:
        raise ToolError(f"'{path}' is inside .git, which Wren's tools never write to. Use git commands instead.")
    return target


def runs_by_itself(ctx: ToolContext, path: Path) -> bool:
    """Whether writing `path` could make something run later, outside
    Wren's control: a direnv `.envrc`, editor tasks, another agent's hooks
    (sandbox.PROTECTED). Writing there is allowed, but auto mode still asks.

    This checks every folder in the path, not just the top one as the
    sandbox does: `sub/.vscode/tasks.json` runs when you open `sub` in an
    editor. Asking too often here costs a question; too rarely, a hole."""
    return any(part in PROTECTED for part in path.relative_to(ctx.root).parts)


def is_inside(ctx: ToolContext, path: Path) -> bool:
    return path.resolve().is_relative_to(ctx.root)


def shown(ctx: ToolContext, path: Path) -> str:
    """How a path is shown to the model: relative to the working directory,
    with forward slashes, so it can be passed straight back to another tool."""
    return path.relative_to(ctx.root).as_posix()


def walk(base: Path) -> Iterator[tuple[Path, list[str], list[str]]]:
    """os.walk, sorted, skipping IGNORED_DIRS. Doesn't follow symlinked folders."""
    for dirpath, dirnames, filenames in os.walk(base):
        # Editing `dirnames` in place is how you tell os.walk not to descend.
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        yield Path(dirpath), dirnames, sorted(filenames)


def is_binary(path: Path) -> bool:
    """Text files almost never contain a zero byte; binary files almost always do."""
    with path.open("rb") as f:
        return b"\0" in f.read(8192)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob pattern into a regex matched against a whole relative path.

        **/   any number of folders, including none
        **    anything at all
        *     anything except a /
        ?     one character except a /

    (Python's own `glob.translate` does this, but only from Python 3.13.)
    """
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def clip(line: str, limit: int) -> str:
    """Shorten a very long line (minified code, data files) so it can't flood the output."""
    if len(line) <= limit:
        return line
    return f"{line[:limit]}… [{len(line) - limit:,} more characters]"


# --- Reading and writing text for edits --------------------------------------
#
# Python's ordinary text mode quietly converts Windows line endings ("\r\n")
# to "\n" when reading. That's fine for showing a file, but an edit tool that
# read a file that way and wrote it back would rewrite every line ending in
# it, and the diff would be the whole file. So edits work on bytes at the
# edges: we note which line ending the file uses, work in "\n" (which is what
# the model writes), and put the original endings back when saving.


def read_text(path: Path, name: str) -> tuple[str, str]:
    """Return (text using "\n", the file's own line ending)."""
    raw = path.read_bytes()
    if b"\0" in raw[:8192]:
        raise ToolError(f"{name} looks like a binary file, so it can't be edited as text.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{name} isn't valid UTF-8 text, so it can't be edited safely.") from None
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), newline


def write_text(path: Path, text: str, newline: str = "\n") -> None:
    if newline != "\n":
        text = text.replace("\n", newline)
    path.write_bytes(text.encode("utf-8"))


def diff(old: str, new: str, name: str) -> str:
    """A unified diff, the same format `git diff` prints: lines starting
    with "-" are removed, "+" are added, and the rest is context."""
    lines = difflib.unified_diff(
        old.splitlines(), new.splitlines(), fromfile=f"a/{name}", tofile=f"b/{name}", lineterm=""
    )
    return "\n".join(lines)


def diff_summary(old: str, new: str) -> str:
    """How big a change is, in lines: "+3 −1"."""
    added = removed = 0
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        added += line.startswith("+ ")
        removed += line.startswith("- ")
    return f"+{added} −{removed}"


def numbered(lines: list[str], first: int, max_chars: int = 500) -> str:
    """Lines in read_file's format: a right-aligned number, a tab, the text."""
    return "\n".join(f"{number:>6}\t{clip(line, max_chars)}" for number, line in enumerate(lines, first))
