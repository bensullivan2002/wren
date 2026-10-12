"""Tools: things the model can ask Wren to do on its behalf.

Three only look (list_files, read_file, search) and run without asking.
Three change things (edit_file, write_file, shell) and ask you first: see
permissions.py.
"""

from __future__ import annotations

from wren.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult
from wren.tools.edit_file import EditFile
from wren.tools.list_files import ListFiles
from wren.tools.permissions import Decision, PermissionRequest, Permissions
from wren.tools.read_file import ReadFile
from wren.tools.registry import ToolRegistry
from wren.tools.search import Search
from wren.tools.shell import Shell
from wren.tools.write_file import WriteFile


def default_tools() -> list[Tool]:
    return [ListFiles(), ReadFile(), Search(), EditFile(), WriteFile(), Shell()]


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
