"""The registry: the set of tools Wren offers, and the one place tools get run.

Every tool call from the model goes through `ToolRegistry.run`, which is
where the rules that apply to *all* tools live:

  1. the tool must exist,
  2. its arguments must match its `Input` model,
  3. if it's about to change something, the user must allow it,
  4. whatever goes wrong, the model gets a readable error result, and the
     agent loop never crashes because of a tool,
  5. no result may be big enough to flood the model's context.

The order of 2 and 3 matters: arguments are checked *before* the user is
asked, so nobody is ever asked to approve a call that couldn't run anyway.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from wren.messages import ToolSpec
from wren.tools.base import Tool, ToolContext, ToolError, ToolResult
from wren.tools.permissions import Permissions

# Every result is re-sent to the model on every later request in the
# conversation, so big results get expensive fast. 50,000 characters is
# roughly 12,000 tokens. Individual tools have tighter limits of their own;
# this is the safety net.
MAX_OUTPUT_CHARS = 50_000

# What the model is told when the user says no. It's written to stop the
# obvious bad reactions: asking again, or reaching the same end another way
# (say, `shell` with `echo ... > file` after a declined edit).
DECLINED = (
    "The user declined this action, so it was not carried out. Don't retry it, "
    "and don't try to get the same result another way. Tell the user what you "
    "wanted to do and why, and ask how they'd like to proceed."
)


class ToolRegistry:
    def __init__(
        self,
        tools: Iterable[Tool] = (),
        root: Path | None = None,
        max_output_chars: int = MAX_OUTPUT_CHARS,
        permissions: Permissions | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"two tools are both named {tool.name!r}")
            self._tools[tool.name] = tool
        self.context = ToolContext(root=(root or Path.cwd()).resolve())
        self.max_output_chars = max_output_chars
        # With no Permissions given, nothing that needs approval can run.
        self.permissions = permissions if permissions is not None else Permissions()

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
        isn't an `Exception`, so the user can always stop a slow tool, or
        back out at a permission prompt.
        """
        tool = self._tools.get(name)
        if tool is None:
            return _error(
                f"There is no tool named '{name}'. Available tools: {', '.join(self._tools)}."
            )
        try:
            checked = tool.parse(args)
            request = tool.permission(checked, self.context)
            if request is not None and not self.permissions.allows(request):
                return ToolResult(DECLINED, summary="declined", is_error=True)
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
