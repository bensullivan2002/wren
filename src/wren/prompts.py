"""The system prompt: standing instructions the model reads before every turn.

The model knows nothing about where it's running: not your operating system,
not your folder, not even today's date. The harness has to tell it. So the
system prompt has two parts: who the model should be (fixed text), and an
<environment> section that the harness fills in from the real world.
"""

from __future__ import annotations

import platform
from datetime import date
from pathlib import Path

IDENTITY = """\
You are Wren, a coding assistant running in the user's terminal.

Right now you can only talk: you cannot read files, edit code, or run commands
yet. If a task needs those, say so plainly and tell the user what to run or
paste. Never pretend to have looked at a file or run a command.

The user reads your replies in a terminal, so keep them concise. Short
paragraphs and plain lists read best; avoid large tables."""


def build_system_prompt(cwd: Path | None = None, today: date | None = None) -> str:
    cwd = cwd or Path.cwd()
    today = today or date.today()
    environment = "\n".join(
        [
            "<environment>",
            f"Working directory: {cwd}",
            f"Platform: {platform.system()} {platform.release()}",
            f"Today's date: {today.isoformat()}",
            "</environment>",
        ]
    )
    return f"{IDENTITY}\n\n{environment}"
