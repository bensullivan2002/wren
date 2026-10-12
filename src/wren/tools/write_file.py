"""write_file: create a file, or replace one completely.

edit_file can only change text that already exists, so it can't make a new
file. This tool can. It will also overwrite an existing file, which is the
right thing for a full rewrite and the wrong thing for a small change: the
description steers the model to edit_file for those, and the permission
prompt shows you a diff of exactly what an overwrite would replace.
"""

from __future__ import annotations

from pydantic import Field

from wren.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult
from wren.tools.files import diff, diff_summary, read_text, resolve_for_writing, shown, write_text
from wren.tools.permissions import FileChange, PermissionRequest


class WriteFile(Tool):
    name = "write_file"
    description = (
        "Create a new text file with the given content, or completely replace an existing one. "
        "Missing parent folders are created. "
        "For a change to part of an existing file, use edit_file instead: it is cheaper and "
        "can't lose the rest of the file. The user is asked to approve each write."
    )

    class Input(ToolInput):
        path: str = Field(description="Path to the file, relative to the working directory.")
        content: str = Field(description="The complete new content of the file.")

    def permission(self, args: Input, ctx: ToolContext) -> PermissionRequest:
        path, name, before, _ = self._plan(args, ctx)
        if before is None:
            title, detail = f"Create {name}", args.content
        else:
            title, detail = f"Overwrite {name}", diff(before, args.content, name)
        return PermissionRequest(
            title=title,
            detail=detail,
            scope="edit",
            scope_label="all file edits",
            change=FileChange(path, before, args.content),
        )

    def run(self, args: Input, ctx: ToolContext) -> ToolResult:
        path, name, before, newline = self._plan(args, ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text(path, args.content, newline)

        count = len(args.content.splitlines())
        lines = "1 line" if count == 1 else f"{count:,} lines"
        change = FileChange(path, before, args.content)
        if before is None:
            return ToolResult(f"Created {name} ({lines}).", summary=f"created {name}, {lines}", change=change)
        return ToolResult(
            f"Overwrote {name} ({lines}).",
            summary=f"overwrote {name} {diff_summary(before, args.content)}",
            change=change,
        )

    def _plan(self, args: Input, ctx: ToolContext):
        """Returns (path, display name, current text or None if new, line ending to write with)."""
        path = resolve_for_writing(ctx, args.path)
        name = shown(ctx, path)
        if path.is_dir():
            raise ToolError(f"{name} is a folder, so it can't be written as a file.")
        if not path.exists():
            return path, name, None, "\n"
        before, newline = read_text(path, name)  # refuses binary files: we won't overwrite those
        return path, name, before, newline
