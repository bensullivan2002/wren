"""Permissions: asking the user before a tool changes anything.

Until step 3, every tool was read-only, so the worst a confused model could
do was waste tokens. Now tools can edit files and run commands, and the model
is still just predicting text: it can misunderstand, or be misled by something
it read in a file. So the harness puts a person between the model's request
and the action.

How it fits together:

  * A tool that changes things describes what it's about to do, as a
    `PermissionRequest` (see `Tool.permission`). Read-only tools return None
    and are never asked about.
  * The registry hands the request to `Permissions.allows()` before running
    the tool.
  * `Permissions` either already knows the answer (you said "always" earlier
    this session) or asks, through a callback the front end supplies.

The callback is what keeps this module free of `input()` and `print()`: the
REPL decides how a question looks, exactly as it decides how events look.

"Always" answers live in memory and are forgotten when Wren exits.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable


@dataclass
class PermissionRequest:
    """What a tool wants to do, described for the person deciding."""

    title: str  # one line: "Edit src/app.py", "Run a shell command"
    detail: str  # the specifics: a diff, or the command itself

    # What "always" would cover. A scope is a plain string key such as "edit"
    # or "shell:pytest"; two requests with the same scope are "the same kind
    # of thing". None means there's no safe way to generalise this request,
    # so "always" isn't offered and it's asked about every time.
    scope: str | None = None
    scope_label: str = ""  # the scope in words: "all file edits"


class Decision(Enum):
    ALLOW = "allow"  # yes, this once
    ALWAYS = "always"  # yes, and stop asking about this scope for the session
    DENY = "deny"


Asker = Callable[[PermissionRequest], Decision]


class Permissions:
    def __init__(self, ask: Asker | None = None) -> None:
        # With nobody to ask (tests, or a front end that hasn't wired itself
        # up), the answer is no. Failing closed means a wiring mistake can
        # never turn into an unapproved edit.
        self.ask = ask
        self.always: dict[str, str] = {}  # scope -> its label, for /permissions

    def allows(self, request: PermissionRequest) -> bool:
        if request.scope is not None and request.scope in self.always:
            return True
        if self.ask is None:
            return False
        decision = self.ask(request)
        if decision is Decision.ALWAYS and request.scope is not None:
            self.always[request.scope] = request.scope_label
        return decision is not Decision.DENY
