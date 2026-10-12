"""search: find text in the project's files (a small, pure-Python grep)."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Iterator

from pydantic import Field

from wren.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult
from wren.tools.files import clip, is_binary, is_inside, resolve, shown, walk

MAX_MATCHES = 100
MAX_LINE_CHARS = 300
MAX_FILE_BYTES = 2_000_000


class Search(Tool):
    name = "search"
    description = (
        "Search the contents of the project's files with a regular expression (Python re syntax). "
        "Returns matching lines as 'path:line_number: text'. "
        "Good for finding where something is defined or used, e.g. 'def run_turn' or 'class \\w+Provider'. "
        "Binary files, files over 2 MB, and folders such as .git and node_modules are skipped."
    )
    class Input(ToolInput):
        pattern: str = Field(description="Regular expression to search for.")
        path: str = Field(".", description="File or folder to search, relative to the working directory.")
        # `str | None` with a default of None is how you say "optional, and
        # there's no sensible value to assume" (unlike `path`, which has one).
        include: str | None = Field(None, description="Only search files whose name matches this glob, e.g. '*.py'.")
        ignore_case: bool = Field(False, description="Match regardless of upper or lower case.")

    def run(self, args: Input, ctx: ToolContext) -> ToolResult:
        try:
            regex = re.compile(args.pattern, re.IGNORECASE if args.ignore_case else 0)
        except re.error as e:
            raise ToolError(f"Invalid regular expression {args.pattern!r}: {e}.")

        base = resolve(ctx, args.path)
        if not base.exists():
            raise ToolError(f"No such file or folder: {args.path}.")

        lines_out: list[str] = []
        total = 0
        files_with_matches = 0

        for path in _candidates(base):
            if args.include and not fnmatch.fnmatch(path.name, args.include):
                continue
            if not _searchable(ctx, path):
                continue
            found_here = False
            with path.open(encoding="utf-8", errors="replace") as f:
                for number, line in enumerate(f, 1):
                    if regex.search(line):
                        total += 1
                        found_here = True
                        if len(lines_out) < MAX_MATCHES:
                            text = clip(line.rstrip("\r\n"), MAX_LINE_CHARS)
                            lines_out.append(f"{shown(ctx, path)}:{number}: {text}")
            files_with_matches += found_here

        if total == 0:
            return ToolResult(f"No matches for {args.pattern!r}.", summary="no matches")

        output = "\n".join(lines_out)
        if total > MAX_MATCHES:
            output += (
                f"\n\n[Showing the first {MAX_MATCHES} of {total:,} matching lines. "
                "Narrow the pattern, path or include to see the rest.]"
            )
        summary = f"{total:,} {'match' if total == 1 else 'matches'} in {files_with_matches} file" + (
            "" if files_with_matches == 1 else "s"
        )
        return ToolResult(output, summary=summary)


def _candidates(base: Path) -> Iterator[Path]:
    if base.is_file():
        yield base
        return
    for folder, _, files in walk(base):
        for name in files:
            yield folder / name


def _searchable(ctx: ToolContext, path: Path) -> bool:
    try:
        # A symlink inside the project could point anywhere: check where it lands.
        if not path.is_file() or not is_inside(ctx, path):
            return False
        return path.stat().st_size <= MAX_FILE_BYTES and not is_binary(path)
    except OSError:  # unreadable, or a broken symlink
        return False
