"""shell: run a command, the tool for everything there's no dedicated tool for.

Tests, git, builds, linters, package managers: nobody can write a dedicated
tool for each, so a coding agent needs a way to run commands. It is also by
far the most powerful thing Wren can do, and three facts follow from that.

1. The fence doesn't apply. The file tools check every path against the
   working directory. A command is just a string handed to your shell, and
   `rm -rf ~` is a perfectly valid string. We start commands *in* the working
   directory, but nothing keeps them there. The protection is you: every
   command is shown to you before it runs. (Real isolation means a sandbox,
   a container or an OS-level jail, which is a project of its own.)

2. "Always allow" has to be careful. You can approve a command's first word
   for the session ("always allow `pytest`"). But `pytest; rm -rf ~` also
   starts with pytest. So that offer is only made, and only honoured, for
   commands with no shell syntax that could chain or redirect. Anything
   cleverer is asked about every time. See `command_word`.

3. Commands can hang. A test that waits for input or a server that never
   exits would freeze the whole agent, so every command has a timeout, gets
   no keyboard input, and is killed together with anything it started.

Each command runs in a fresh shell: `cd` and variables don't carry over to
the next call. That's simpler and more predictable than keeping one shell
session alive, at the cost of the model writing `cd sub && make` sometimes.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess

from pydantic import Field

from wren.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult
from wren.tools.permissions import PermissionRequest

DEFAULT_TIMEOUT = 120  # seconds
MAX_TIMEOUT = 600
MAX_OUTPUT_CHARS = 20_000

# Characters that let one command line do more than run one program:
# chaining (; & |), substitution (` $), redirection (< >), grouping, and
# line breaks. A command containing any of them is never covered by "always".
_SHELL_SYNTAX = set(";&|`$<>(){}\n\r\\")


class Shell(Tool):
    name = "shell"
    description = (
        "Run a shell command in the working directory and return its output (stdout and stderr together) "
        "and its exit code if it failed. Use it to run tests, git, builds and other programs. "
        "Each call starts a fresh shell, so 'cd' and variables do not carry over between calls: "
        "chain with && when you need to. Commands get no keyboard input, so avoid anything interactive, "
        f"and are stopped after the timeout ({DEFAULT_TIMEOUT} seconds unless you set it). "
        "Don't start servers or other programs that never exit. "
        "Prefer list_files, read_file, search and edit_file to ls, cat, grep and sed: their output is "
        "shaped for you and they need less approval. The user is asked to approve commands."
    )

    class Input(ToolInput):
        command: str = Field(description="The command line to run.")
        timeout: int = Field(
            DEFAULT_TIMEOUT, ge=1, le=MAX_TIMEOUT, description="Seconds to wait before stopping the command."
        )

    def permission(self, args: Input, ctx: ToolContext) -> PermissionRequest:
        if not args.command.strip():
            raise ToolError("command is empty.")
        word = command_word(args.command)
        return PermissionRequest(
            title="Run a shell command",
            detail=args.command,
            scope=f"shell:{word}" if word else None,
            scope_label=f"commands starting with `{word}`" if word else "",
        )

    def run(self, args: Input, ctx: ToolContext) -> ToolResult:
        output, exit_code, timed_out = _run(args.command, ctx.root, args.timeout)

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

        if timed_out:
            content += f"\n\n[Stopped: the command was still running after {args.timeout} seconds.]"
            return ToolResult(content, summary=f"timed out after {args.timeout}s", is_error=True)
        if exit_code != 0:
            # Not marked as an error: the tool worked, and a failing test run
            # is exactly the information the model asked for.
            return ToolResult(f"{content}\n\n[Exit code {exit_code}]", summary=f"exit {exit_code} · {size}")
        return ToolResult(content, summary=size)


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


def _run(command: str, cwd, timeout: int) -> tuple[str, int, bool]:
    """Run `command` in a shell. Returns (output, exit code, whether it timed out)."""
    posix = os.name == "posix"
    process = subprocess.Popen(
        command,
        shell=True,
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
    try:
        output, _ = process.communicate(timeout=timeout)
        return output, process.returncode, False
    except subprocess.TimeoutExpired:
        _kill(process, posix)
        output, _ = process.communicate()  # collect whatever it printed before it was stopped
        return output, process.returncode, True
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
