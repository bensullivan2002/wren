"""Tools: things the model can ask Wren to do on its behalf.

Step 2's tools are all read-only: the model can look, but not touch.
"""

from __future__ import annotations

from wren.tools.base import Tool, ToolContext, ToolError, ToolResult
from wren.tools.list_files import ListFiles
from wren.tools.read_file import ReadFile
from wren.tools.registry import ToolRegistry
from wren.tools.search import Search


def default_tools() -> list[Tool]:
    return [ListFiles(), ReadFile(), Search()]


__all__ = [
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "default_tools",
]
