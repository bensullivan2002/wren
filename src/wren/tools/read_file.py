"""read_file: show the model a file, with line numbers, a page at a time."""

from __future__ import annotations

from wren.tools.base import Tool, ToolContext, ToolError, ToolResult
from wren.tools.files import clip, is_binary, resolve, shown

DEFAULT_LIMIT = 500  # lines per read
MAX_LINE_CHARS = 500
MAX_FILE_BYTES = 5_000_000


class ReadFile(Tool):
    name = "read_file"
    description = (
        "Read a text file in the project. Each line is returned prefixed with its line "
        "number and a tab; the numbers are not part of the file. "
        f"Returns at most {DEFAULT_LIMIT} lines per call: for longer files, the output "
        "ends with a note saying which offset to use to read the next part. "
        "If you don't know the exact path, use list_files or search first."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file, relative to the working directory.",
            },
            "offset": {
                "type": "integer",
                "minimum": 1,
                "default": 1,
                "description": "Line number to start reading from (1 is the first line).",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "default": DEFAULT_LIMIT,
                "description": "Maximum number of lines to return.",
            },
        },
        "required": ["path"],
    }

    def run(self, args, ctx: ToolContext) -> ToolResult:
        path = resolve(ctx, args["path"])
        name = shown(ctx, path)

        if not path.exists():
            raise ToolError(f"No such file: {name}. Use list_files to see what exists.")
        if path.is_dir():
            raise ToolError(f"{name} is a folder, not a file. Use list_files to see what's in it.")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ToolError(f"{name} is too large to read ({size:,} bytes). Use search to find the part you need.")
        if is_binary(path):
            raise ToolError(f"{name} looks like a binary file, so it can't be shown as text.")

        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        if total == 0:
            return ToolResult("(this file is empty)", summary="empty file")

        start = args["offset"]
        if start > total:
            raise ToolError(f"offset {start} is past the end of {name}, which has {total} lines.")
        end = min(start - 1 + args["limit"], total)

        # Line numbers let the model say "line 42", and (from step 3) point
        # precisely at the code it wants to change.
        numbered = "\n".join(
            f"{number:>6}\t{clip(line, MAX_LINE_CHARS)}"
            for number, line in enumerate(lines[start - 1 : end], start)
        )
        if start == 1 and end == total:
            return ToolResult(numbered, summary=f"{total} lines")

        # A note that tells the model how to get the rest. Tool output is the
        # model's only view of the world, so make it say what to do next.
        note = f"[Showing lines {start}-{end} of {total}."
        if end < total:
            note += f" To read on, call read_file with offset={end + 1}."
        note += "]"
        return ToolResult(f"{numbered}\n\n{note}", summary=f"lines {start}-{end} of {total}")
