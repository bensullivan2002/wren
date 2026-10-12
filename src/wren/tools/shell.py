"""shell: run a command, the tool for everything there's no dedicated tool for.

Tests, git, builds, linters, package managers: nobody can write a dedicated
tool for each, so a coding agent needs a way to run commands. It is also by
far the most powerful thing Wren can do, and four facts follow from that.

1. The fence doesn't apply. The file tools check every path against the
   working directory. A command is just a string handed to your shell, and
   `rm -rf ~` is a perfectly valid string. So on macOS every command runs
   in a sandbox (sandbox.py): the operating system lets it write only
   inside the working directory and temp folders, and gives it no network.
   A command that needs more (installing packages, `git commit`, `git push`)
   has to ask for `outside_sandbox`, and is always asked about, even in auto
   mode. Elsewhere there's no sandbox yet, and the protection is you: every
   command is shown to you before it runs.

2. "Always allow" has to be careful. You can approve a command's first word
   for the session ("always allow `pytest`"). But `pytest; rm -rf ~` also
   starts with pytest. So that offer is only made, and only honoured, for
   commands with no shell syntax that could chain or redirect. Anything
   cleverer is asked about every time. See `command_word`.

3. Commands can hang. A test that waits for input or a server that never
   exits would freeze the whole agent, so every command has a timeout, gets
   no keyboard input, and is killed together with anything it started. It
   is also the one tool slow enough to need stopping part-way, so it checks
   `ctx.should_stop` a few times a second while it waits.

4. A command can be unreadable. `grep -rl TODO src | xargs sed -i ...` is a
   fine thing to run and a hopeless thing to be asked about: approving what
   you can't read isn't approval. So the model has to say, in plain English,
   what each command is for, and that sentence leads the permission prompt.
   It's the model's own claim, and a model can be wrong or misled, so the
   command itself is always shown with it. The sentence is there to help
   you read the command, not to be believed in its place.

Each command runs in a fresh shell: `cd` and variables don't carry over to
the next call. That's simpler and more predictable than keeping one shell
session alive, at the cost of the model writing `cd sub && make` sometimes.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from typing import Callable

from pydantic import Field

from wren.tools import sandbox
from wren.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult
from wren.tools.permissions import PermissionRequest

DEFAULT_TIMEOUT = 120  # seconds
MAX_TIMEOUT = 600
MAX_OUTPUT_CHARS = 20_000
POLL_SECONDS = 0.2  # how often a running command checks whether to stop

# Characters that let one command line do more than run one program:
# chaining (; & |), substitution (` $), redirection (< >), grouping, and
# line breaks. A command containing any of them is never covered by "always".
_SHELL_SYNTAX = set(";&|`$<>(){}\n\r\\")

_PROTECTED = ", ".join(sandbox.PROTECTED)

# What the model is told about the sandbox: in the tool's description, and
# again when a sandboxed command fails, which is when it needs to know.
_SANDBOX_RULES = (
    "Commands run in a sandbox: they can read files, but can write only inside the working directory "
    f"and temp folders (not {_PROTECTED}), and have no network. For a command that needs more, such as "
    "installing packages, git commit or git push, set outside_sandbox; the user is always asked about those. "
)
SANDBOX_NOTE = (
    "[This command ran in the sandbox: no network, and writes only inside the working directory and temp "
    f"folders, not {_PROTECTED}. If that is why it failed, run it again with outside_sandbox set to true. "
    "The user will be asked.]"
)


class Shell(Tool):
    name = "shell"
    description = (
        "Run a shell command in the working directory and return its output (stdout and stderr together) "
        "and its exit code if it failed. Use it to run tests, git, builds and other programs. "
        "Each call starts a fresh shell, so 'cd' and variables do not carry over between calls: "
        "chain with && when you need to. Commands get no keyboard input, so avoid anything interactive, "
        f"and are stopped after the timeout ({DEFAULT_TIMEOUT} seconds unless you set it). "
        "Don't start servers or other programs that never exit. "
        + (_SANDBOX_RULES if sandbox.available() else "")
        + "Prefer list_files, read_file, search and edit_file to ls, cat, grep and sed: their output is "
        "shaped for you and they need less approval. The user may be asked to approve a command, and sees "
        "your description first: without a clear one they can't judge the command, and will likely decline."
    )

    class Input(ToolInput):
        description: str = Field(
            min_length=5,
            description=(
                "What this command does and why, in one plain-English sentence that someone who "
                "doesn't read shell could act on. Say what it will change, if anything. "
                "For example: 'Count the lines in every Python file under src, to find the largest.' "
                "or 'Delete the build folder so the next build starts clean.'"
            ),
        )
        command: str = Field(description="The command line to run.")
        timeout: int = Field(
            DEFAULT_TIMEOUT, ge=1, le=MAX_TIMEOUT, description="Seconds to wait before stopping the command."
        )
        outside_sandbox: bool = Field(
            False,
            description=(
                "Run without the sandbox, with network access and everything the user can do. "
                "Only for a command the sandbox would stop. The user is always asked first."
            ),
        )

    def permission(self, args: Input, ctx: ToolContext) -> PermissionRequest:
        if not args.command.strip():
            raise ToolError("command is empty.")
        word = command_word(args.command)
        if not sandbox.available():  # nothing to be inside or outside of
            title, scope, label = "Run a shell command", f"shell:{word}", f"commands starting with `{word}`"
        elif args.outside_sandbox:
            # Its own scope: "always allow `uv`" in the sandbox mustn't cover `uv` outside it.
            title, scope = "Run a shell command outside the sandbox", f"shell-outside:{word}"
            label = f"commands starting with `{word}`, outside the sandbox"
        else:
            title, scope, label = "Run a shell command in the sandbox", f"shell:{word}", f"commands starting with `{word}`"
        return PermissionRequest(
            title=title,
            explanation=args.description.strip(),
            detail=args.command,
            scope=scope if word else None,
            scope_label=label if word else "",
            contained=_sandboxed(args),
        )

    def run(self, args: Input, ctx: ToolContext) -> ToolResult:
        sandboxed = _sandboxed(args)
        command = sandbox.command(args.command, ctx.root) if sandboxed else args.command
        output, exit_code, stopped = _run(command, ctx.root, args.timeout, ctx.should_stop)

        # For a command, the end of the output is usually what matters (the
        # error, the test summary), so when it's too long we keep the tail.
        if len(output) > MAX_OUTPUT_CHARS:
            output = (
                f"[Output truncated: showing the last {MAX_OUTPUT_CHARS:,} of {len(output):,} characters.]\n"
                + output[-MAX_OUTPUT_CHARS:]
            )
        count = len(output.splitlines())
        size = "no output" if count == 0 else "1 line" if count == 1 else f"{count:,} lines"
        content = output.rstrip("\n") or "(no output)"

        if stopped == "timeout":
            content += f"\n\n[Stopped: the command was still running after {args.timeout} seconds.]"
            return ToolResult(content, summary=f"timed out after {args.timeout}s", is_error=True)
        if stopped == "cancelled":
            content += "\n\n[Stopped by the user before it finished. It may have partly completed.]"
            return ToolResult(content, summary="stopped", is_error=True)
        if exit_code != 0:
            # Not marked as an error: the tool worked, and a failing test run
            # is exactly the information the model asked for.
            content += f"\n\n[Exit code {exit_code}]"
            # The sandbox's refusals look like any other failure ("Operation
            # not permitted", "Could not resolve host"), so say it was there.
            if sandboxed:
                content += f"\n{SANDBOX_NOTE}"
            return ToolResult(content, summary=f"exit {exit_code} · {size}")
        return ToolResult(content, summary=size)


def _sandboxed(args: Shell.Input) -> bool:
    """Whether this call runs in the sandbox. permission() and run() both
    ask, so what you're asked about is what runs."""
    return sandbox.available() and not args.outside_sandbox


def command_word(command: str) -> str | None:
    """The program a simple command runs ("pytest" for "pytest -q tests"), or
    None if the command isn't simple enough for "always allow" to be safe."""
    if _SHELL_SYNTAX & set(command):
        return None
    try:
        words = shlex.split(command)  # splits like a shell does, respecting quotes
    except ValueError:  # e.g. an unclosed quote
        return None
    # "FOO=1 pytest" sets a variable for the command; its first word isn't the program.
    if not words or "=" in words[0]:
        return None
    return words[0]


def _run(
    command: str | list[str], cwd, timeout: int, should_stop: Callable[[], bool]
) -> tuple[str, int, str | None]:
    """Run `command`: a command line for the shell, or a list of arguments
    (the sandboxed form, which starts its own shell inside the sandbox).
    Returns (output, exit code, why it was stopped early: "timeout",
    "cancelled", or None if it finished)."""
    posix = os.name == "posix"
    process = subprocess.Popen(
        command,
        shell=isinstance(command, str),
        cwd=cwd,
        stdin=subprocess.DEVNULL,  # anything that tries to read input gets "end of file" at once
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # interleave errors with output, as a terminal would
        text=True,
        errors="replace",
        # A new "session" puts the shell and everything it starts into one
        # process group that we can kill together. It also means Ctrl-C in
        # the terminal reaches Wren only, and Wren decides what to stop.
        start_new_session=posix,
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                # Wait a moment for it to finish. communicate() can be called
                # again after a timeout without losing any output.
                output, _ = process.communicate(timeout=POLL_SECONDS)
                return output, process.returncode, None
            except subprocess.TimeoutExpired:
                stopped = "cancelled" if should_stop() else "timeout" if time.monotonic() >= deadline else None
            if stopped:
                _kill(process, posix)
                output, _ = process.communicate()  # collect whatever it printed before it was stopped
                return output, process.returncode, stopped
    except BaseException:  # Ctrl-C while the command runs: don't leave it running behind us
        _kill(process, posix)
        process.wait()
        raise


def _kill(process: subprocess.Popen, posix: bool) -> None:
    try:
        if posix:
            # Killing only the shell would leave its children (the actual
            # test run, say) alive and holding the output pipe open.
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:  # it finished in the meantime
        pass
