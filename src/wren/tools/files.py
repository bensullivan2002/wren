"""Helpers shared by the file tools: keeping paths inside the working
directory, walking folders, spotting binary files, and matching globs."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterator

from wren.tools.base import ToolContext, ToolError

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
