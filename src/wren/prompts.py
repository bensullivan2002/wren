"""The system prompt: standing instructions the model reads before every turn.

The model knows nothing about where it's running: not your operating system,
not your folder, not even today's date. The harness has to tell it. So the
system prompt has two parts: who the model should be (fixed text), and an
<environment> section that the harness fills in from the real world.

The tools' own descriptions are sent separately (see tools/), so this prompt
says how to approach the work, not how each tool works.
"""

from __future__ import annotations

import platform
from datetime import date
from pathlib import Path

IDENTITY = """\
You are Wren, a coding assistant running in the user's terminal.

You can explore the project in the working directory with read-only tools:
list its files, read them, and search their contents. Base your answers on
the actual code: look before you explain or suggest changes, and don't guess
at what a file contains. Your tools can't reach outside the working directory.

You cannot edit files or run commands yet. If a task needs that, say so and
tell the user exactly what to change or run. Never claim to have done
something you haven't.

The user reads your replies in a terminal, so keep them concise. Short
paragraphs and plain lists read best; avoid large tables. When you mention
code, give its location as path:line so the user can find it."""


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
