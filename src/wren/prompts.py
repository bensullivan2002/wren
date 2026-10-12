"""The system prompt: standing instructions the model reads before every turn.

The model knows nothing about where it's running: not your operating system,
not your folder, not even today's date. The harness has to tell it. So the
system prompt has three parts: who the model should be (fixed text), the
project's memory files if there are any (see memory.py), and an
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

You can explore the project in the working directory: list its files, read
them, and search their contents. Base your answers on the actual code: look
before you explain or change anything, and don't guess at what a file
contains. Your file tools can't reach outside the working directory.

You can also change the project: edit and create files, and run shell
commands such as tests and git. Read a file before you edit it. Make the
smallest change that does the job, and after changing code, run the
project's tests if it has them.

The user is asked to approve each edit and command, and may decline. If they
do, don't retry or work around it: say what you wanted to do and ask how
they'd like to proceed. Never claim to have done something you haven't.

The user reads your replies in a terminal, so keep them concise. Short
paragraphs and plain lists read best; avoid large tables. When you mention
code, give its location as path:line so the user can find it."""


# --- Compaction -----------------------------------------------------------------
# When a conversation gets long, Wren asks the model to write a summary of it
# and carries on from the summary alone (see Agent.compact). The quality of
# everything afterwards depends on this prompt: whatever it doesn't ask for
# is forgotten. So it asks for the things a colleague taking over would need.

COMPACT_PROMPT = """\
This conversation is getting long, so it is about to be replaced by a summary. \
Write that summary now. After this, you will have ONLY the summary to work from: \
anything you leave out is lost, and you will not be able to see the messages above.

Do not use any tools. Reply with the summary only, under these headings:

## What the user wants
Their goals and requests, in their own words where the wording matters. Include \
any preferences or instructions they gave about how to work.

## What has been done
What was changed or run, in order: files edited or created (with paths), \
commands run and how they turned out. Say what the user approved and what they declined.

## What was learned
Facts about the project that took effort to find: where things live \
(path:line), how the relevant code works, causes of bugs, dead ends already ruled out.

## Current state
What is finished, what is half-done, and anything known to be broken or untested.

## Next steps
What you were about to do, and anything still waiting on the user.

Be specific and concrete. Keep exact names, paths, commands and error messages; \
leave out pleasantries and the details of how you searched."""

# What stands in the history where the old conversation was. It's phrased as
# a note in the user's place, so that the summary (the assistant's reply to
# it) reads naturally as what comes next.
COMPACTED_NOTE = (
    "[The earlier part of this conversation was removed to save space. "
    "The assistant's own summary of it follows. Files may have changed since: "
    "read them again before relying on details.]"
)


def build_system_prompt(cwd: Path | None = None, today: date | None = None, memory: str = "") -> str:
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
    # Ordered from what changes least to what changes most. The cache
    # matches from the front, so the stable text should come first.
    return "\n\n".join(part for part in (IDENTITY, memory, environment) if part)
