"""The registry: the set of tools Wren offers, and the one place tools get run.

Every tool call from the model goes through `ToolRegistry.run`, which is
where the rules that apply to *all* tools live:

  1. the tool must exist,
  2. its arguments must match its schema,
  3. whatever goes wrong, the model gets a readable error result, and the
     agent loop never crashes because of a tool,
  4. no result may be big enough to flood the model's context.

Step 3 adds one more rule here: ask the user before anything risky runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from wren.messages import ToolSpec
from wren.tools.base import Tool, ToolContext, ToolError, ToolResult
from wren.tools.validation import validate_args

# Every result is re-sent to the model on every later request in the
# conversation, so big results get expensive fast. 50,000 characters is
# roughly 12,000 tokens. Individual tools have tighter limits of their own;
# this is the safety net.
MAX_OUTPUT_CHARS = 50_000


class ToolRegistry:
    def __init__(
        self,
        tools: Iterable[Tool] = (),
        root: Path | None = None,
        max_output_chars: int = MAX_OUTPUT_CHARS,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"two tools are both named {tool.name!r}")
            self._tools[tool.name] = tool
        self.context = ToolContext(root=(root or Path.cwd()).resolve())
        self.max_output_chars = max_output_chars

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self):
        return iter(self._tools.values())

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        """What we tell the model about every tool, sent with each request."""
        return [tool.spec() for tool in self._tools.values()]

    def run(self, name: str, args: Any) -> ToolResult:
        """Run one tool call. Returns an error result rather than raising.

        The only exception that escapes is KeyboardInterrupt (Ctrl-C), which
        isn't an `Exception`, so the user can always stop a slow tool.
        """
        tool = self._tools.get(name)
        if tool is None:
            return _error(f"There is no tool named '{name}'. Available tools: {', '.join(self._tools)}.")
        try:
            checked = validate_args(args, tool.input_schema)
            result = tool.run(checked, self.context)
        except ToolError as e:
            return _error(str(e))
        except Exception as e:  # a bug in the tool, or an OS error it didn't expect
            return _error(f"{name} failed unexpectedly: {type(e).__name__}: {e}")
        return self._cap(result)

    def _cap(self, result: ToolResult) -> ToolResult:
        size = len(result.content)
        if size <= self.max_output_chars:
            return result
        result.content = (
            result.content[: self.max_output_chars]
            + f"\n\n[Output truncated: showing the first {self.max_output_chars:,} of {size:,} characters.]"
        )
        return result


def _error(message: str) -> ToolResult:
    first_line = message.splitlines()[0] if message else "error"
    return ToolResult(content=message, summary=first_line, is_error=True)
