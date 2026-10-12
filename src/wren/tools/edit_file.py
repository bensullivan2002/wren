"""edit_file: change part of a file by replacing one exact piece of text.

There are several ways to let a model edit a file, and this "search and
replace" form is the one most coding agents have settled on. The model quotes
the text that's there now (`old_string`) and gives what should be there
instead (`new_string`). Compared with the alternatives:

  * Rewriting the whole file costs output tokens for every unchanged line,
    and models asked to copy 400 lines sometimes "tidy up" a few on the way.
  * Line numbers ("replace lines 40-42") go stale the moment an earlier edit
    adds or removes a line.
  * Quoting the text can't go stale silently: if the file isn't what the
    model thinks it is, `old_string` isn't found and the edit fails loudly.

That last property is the point. The two strict rules below (the text must
be there, and must be there exactly once) turn "the model was working from
an out-of-date picture of the file" from a silent wrong edit into an error
it can read and recover from.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from wren.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult
from wren.tools.files import diff, diff_summary, numbered, read_text, resolve_for_writing, shown, write_text
from wren.tools.permissions import FileChange, PermissionRequest

CONTEXT_LINES = 3  # lines shown either side of the change in the result


class EditFile(Tool):
    name = "edit_file"
    description = (
        "Change an existing text file by replacing an exact piece of text with new text. "
        "old_string must match the file character for character, including indentation and line breaks, "
        "and must not include the line-number prefixes that read_file shows. "
        "By default old_string must occur exactly once: if it occurs more often, include more of the "
        "surrounding lines to make it unique, or set replace_all to change every occurrence. "
        "Read the file first so you are quoting what is really there. "
        "To create a new file, use write_file. The user is asked to approve each edit."
    )

    class Input(ToolInput):
        path: str = Field(description="Path to the file, relative to the working directory.")
        old_string: str = Field(description="The exact text to replace.")
        new_string: str = Field(description="The text to put in its place. May be empty, to delete old_string.")
        replace_all: bool = Field(False, description="Replace every occurrence of old_string, not just one.")

    def permission(self, args: Input, ctx: ToolContext) -> PermissionRequest:
        # Working out the edit here, before asking, does two jobs: an edit
        # that can't succeed fails now instead of bothering the user, and
        # the user is shown the real diff rather than the model's arguments.
        plan = self._plan(args, ctx)
        return PermissionRequest(
            title=f"Edit {plan.name}",
            detail=diff(plan.before, plan.after, plan.name),
            scope="edit",
            scope_label="all file edits",
            change=plan.change,
        )

    def run(self, args: Input, ctx: ToolContext) -> ToolResult:
        # Planned again from scratch, on purpose. The user may have taken a
        # while to answer, and the file could have changed in the meantime;
        # this way we edit the file as it is now, or fail if it no longer fits.
        plan = self._plan(args, ctx)
        write_text(plan.path, plan.after, plan.newline)

        # Show the model the result, so it can check its work without
        # spending another call on read_file.
        lines = plan.after.splitlines()
        first = max(plan.line - CONTEXT_LINES, 1)
        last = min(plan.line + args.new_string.count("\n") + CONTEXT_LINES, len(lines))
        times = "1 occurrence" if plan.count == 1 else f"all {plan.count} occurrences"
        content = f"Edited {plan.name}: replaced {times}."
        if lines:
            where = "Around the change" if plan.count == 1 else "Around the first change"
            content += f" {where}, the file now reads:\n{numbered(lines[first - 1 : last], first)}"
        return ToolResult(content, summary=f"{plan.name} {diff_summary(plan.before, plan.after)}", change=plan.change)

    def _plan(self, args: Input, ctx: ToolContext) -> _Plan:
        path = resolve_for_writing(ctx, args.path)
        name = shown(ctx, path)
        if path.is_dir():
            raise ToolError(f"{name} is a folder, not a file.")
        if not path.exists():
            raise ToolError(f"No such file: {name}. To create it, use write_file.")
        if args.old_string == "":
            raise ToolError("old_string is empty. Quote the text you want to replace, or use write_file for a new file.")
        if args.old_string == args.new_string:
            raise ToolError("old_string and new_string are the same, so there is nothing to change.")

        before, newline = read_text(path, name)
        count = before.count(args.old_string)
        if count == 0:
            raise ToolError(
                f"old_string was not found in {name}. It must match exactly, including spaces and "
                "indentation. Read the file again to see its current contents."
            )
        if count > 1 and not args.replace_all:
            raise ToolError(
                f"old_string occurs {count} times in {name} (at lines {_lines_of(before, args.old_string)}). "
                "Include more of the surrounding lines to pick one, or set replace_all to change them all."
            )
        after = before.replace(args.old_string, args.new_string)
        line = before[: before.index(args.old_string)].count("\n") + 1
        return _Plan(path, name, before, after, newline, count, line)


@dataclass
class _Plan:
    """A worked-out edit: everything needed to show it, and to carry it out."""

    path: Path
    name: str  # the path as shown to the model and the user
    before: str  # the file's text now
    after: str  # ...and as it will be
    newline: str  # the file's own line ending, to write it back with
    count: int  # how many replacements
    line: int  # the line the first one starts on

    @property
    def change(self) -> FileChange:
        return FileChange(self.path, self.before, self.after)


def _lines_of(text: str, needle: str, limit: int = 5) -> str:
    """The line numbers where `needle` starts: "12, 40, 97"."""
    found, start = [], 0
    while (index := text.find(needle, start)) != -1:
        found.append(text[:index].count("\n") + 1)
        start = index + len(needle)
    shown_lines = ", ".join(str(n) for n in found[:limit])
    return shown_lines + (", …" if len(found) > limit else "")
