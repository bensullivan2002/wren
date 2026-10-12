"""list_files: let the model see what's in the project."""

from __future__ import annotations

from pydantic import Field

from wren.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult
from wren.tools.files import glob_to_regex, resolve, shown, walk

MAX_RESULTS = 200


class ListFiles(Tool):
    name = "list_files"
    description = (
        "List files and folders in the project, optionally filtered by a glob pattern. "
        "Folders end with '/'. Paths are relative to the working directory, ready to pass to read_file. "
        "In patterns, '*' matches within one folder and '**/' matches any number of folders. "
        "Examples: '*' lists one folder; '**/*.py' finds Python files at any depth; "
        "'src/**/test_*.py' finds test files under src. "
        "Folders such as .git, node_modules and .venv are skipped."
    )

    class Input(ToolInput):
        pattern: str = Field(
            "*", description="Glob pattern, matched against paths relative to `path`."
        )
        path: str = Field(
            ".", description="Folder to look in, relative to the working directory."
        )

    def run(self, args: Input, ctx: ToolContext) -> ToolResult:
        base = resolve(ctx, args.path)
        pattern = args.pattern
        if not base.exists():
            raise ToolError(f"No such folder: {args.path}.")
        if not base.is_dir():
            raise ToolError(
                f"{shown(ctx, base)} is a file, not a folder. Use read_file to read it."
            )

        regex = glob_to_regex(pattern)
        # A pattern without a '/' (like '*.py') only looks in the one folder,
        # just as it would in your shell. Walking the whole tree for it would
        # be slow, and surprising.
        recursive = "/" in pattern or "**" in pattern

        matches = []
        for folder, subfolders, files in walk(base):
            relative_folder = folder.relative_to(base)
            entries = [(d, True) for d in subfolders] + [(f, False) for f in files]
            for entry, is_folder in entries:
                if regex.fullmatch((relative_folder / entry).as_posix()):
                    matches.append(
                        shown(ctx, folder / entry) + ("/" if is_folder else "")
                    )
            if not recursive:
                break

        where = shown(ctx, base)
        if not matches:
            return ToolResult(
                f"Nothing in {where} matches '{pattern}'.", summary="no matches"
            )

        output = "\n".join(matches[:MAX_RESULTS])
        if len(matches) > MAX_RESULTS:
            output += (
                f"\n\n[Showing {MAX_RESULTS} of {len(matches):,} matches. "
                "Use a narrower pattern or path to see the rest.]"
            )
        noun = "item" if len(matches) == 1 else "items"
        return ToolResult(output, summary=f"{len(matches):,} {noun}")
