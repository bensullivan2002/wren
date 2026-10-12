"""What a tool is, and the small pieces every tool shares.

A tool has two halves:

  * what the MODEL sees: a name, a description and a JSON Schema for its
    arguments (`spec()`). That's all the model ever knows about it, so the
    description is effectively a prompt: it decides when and how the model
    uses the tool.
  * what WREN runs: the `run()` method, ordinary Python.

The model asks for a tool by name with some JSON arguments; the registry
checks the arguments against the schema, calls `run()`, and sends the result
back. See registry.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wren.messages import ToolSpec


class ToolError(Exception):
    """Raise this from a tool to report a problem to the model.

    The message is sent back as the tool's result (marked as an error), so
    write it for the model: say what went wrong and what to try instead.
    """


@dataclass
class ToolResult:
    content: str  # what the model sees
    summary: str = ""  # one short line for the person watching the terminal
    is_error: bool = False


@dataclass
class ToolContext:
    """What a tool is allowed to know about its surroundings."""

    root: Path  # the working directory, fully resolved; tools must stay inside it


class Tool:
    """Base class for tools. Subclasses set the three class attributes and implement run()."""

    name: str
    description: str
    input_schema: dict[str, Any]

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.input_schema)

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Do the work. `args` has already been checked against input_schema,
        with defaults filled in. Raise ToolError for problems the model should hear about."""
        raise NotImplementedError
