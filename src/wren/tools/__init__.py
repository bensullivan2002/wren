"""Tools: things the model can ask Wren to do on its behalf.

Four only look (list_files, read_file, search, github_read) and never ask.
Edits ask unless contained in auto mode; shell commands ask unless sandboxed.
See permissions.py.
"""

from __future__ import annotations

from wren.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult
from wren.tools.edit_file import EditFile
from wren.tools.github_read import GitHubRead
from wren.tools.list_files import ListFiles
from wren.tools.permissions import Decision, PermissionRequest, Permissions
from wren.tools.read_file import ReadFile
from wren.tools.registry import ToolRegistry
from wren.tools.search import Search
from wren.tools.shell import Shell
from wren.tools.write_file import WriteFile


def default_tools() -> list[Tool]:
    return [
        ListFiles(),
        ReadFile(),
        Search(),
        EditFile(),
        WriteFile(),
        Shell(),
        GitHubRead(),
    ]


__all__ = [
    "Decision",
    "PermissionRequest",
    "Permissions",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolInput",
    "ToolRegistry",
    "ToolResult",
    "default_tools",
]
