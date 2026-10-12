"""The sandbox: running a shell command where it can't do much harm.

The permission prompt was the only thing standing between the model and your
machine (D29). That works only if every prompt is read, and prompts that
come every few seconds stop being read. A sandbox protects you whether you
read or not. It is a rule the operating system enforces, so neither a model
that has misunderstood nor text planted in a file can talk its way past it.

What a sandboxed command can do:

  * read files anywhere, except the folders where credentials live
  * write only inside the working directory and the temporary folders,
    and not to the files there that make something run by itself later
    (PROTECTED: git hooks, direnv, editor tasks, other agents' settings)
  * run programs, which inherit the same limits
  * nothing on the network

How: macOS has a sandbox built into its kernel, called Seatbelt. A profile
written in SBPL (a small Lisp) lists what's allowed, and `sandbox-exec`
applies it to a program and everything that program starts. Apple marks
`sandbox-exec` as deprecated but ships it and uses it widely; Codex CLI and
Claude Code both run commands this way on macOS.

The profile starts from "deny everything" and allows what ordinary
command-line programs need. Starting from "allow everything" and denying the
dangerous parts is shorter, but leaves holes: a command could ask macOS to
open an app, and the app would run outside the sandbox. The list of system
services below follows the profiles Codex (Apache-2.0) and Anthropic's
sandbox-runtime (Apache-2.0) use, trimmed to what a terminal command needs.

Paths are never pasted into the profile's text. They're passed separately
as parameters (`-D NAME=path`, read with `(param "NAME")`), so a folder
whose name contains a quote can't change what the profile says.

On other systems there's no sandbox (yet): `available()` is False, commands
run as before, and auto mode still asks before every command.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Names at the top of a project that make something run later by itself,
# outside the sandbox: git runs hooks and config-named programs; direnv runs
# .envrc when you cd in; editors run tasks when a folder opens; other coding
# agents run hooks from their settings. Sandboxed commands can't write them,
# and in auto mode, edits to them still ask.
PROTECTED = (".git", ".envrc", ".vscode", ".idea", ".claude", ".codex")

# Folders and files in your home directory that hold keys and tokens. With
# no network a command couldn't send them anywhere, but whatever it prints
# goes to the model, and into the session file.
CREDENTIALS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".netrc",
    ".config/gh",
    ".docker",
    ".kube",
    ".pypirc",
    ".npmrc",
    "Library/Keychains",
)


def available() -> bool:
    """Whether commands can be sandboxed on this machine."""
    return sys.platform == "darwin" and os.path.exists(SANDBOX_EXEC)


def command(line: str, root: Path) -> list[str]:
    """The arguments that run the shell command `line` in the sandbox,
    for `subprocess.Popen` (without `shell=True`)."""
    params = _params(root)
    flags = [f"-D{name}={value}" for name, value in params.items()]
    return [SANDBOX_EXEC, "-p", _profile(params), *flags, "/bin/sh", "-c", line]


def temp_dirs() -> list[str]:
    """The temporary folders commands may write to. Compilers, test runners
    and package tools all expect to."""
    return [tempfile.gettempdir(), "/tmp"]


def _params(root: Path) -> dict[str, str]:
    # Seatbelt matches real paths, after symlinks: on macOS /tmp is really
    # /private/tmp, and a profile that said /tmp would match nothing.
    real = os.path.realpath
    home = Path.home()
    params = {"ROOT": real(root)}
    params.update({f"TEMP_{i}": real(path) for i, path in enumerate(temp_dirs())})
    params.update(
        {f"PROTECTED_{i}": real(root / name) for i, name in enumerate(PROTECTED)}
    )
    params.update(
        {f"SECRET_{i}": real(home / name) for i, name in enumerate(CREDENTIALS)}
    )
    return params


def _profile(params: dict[str, str]) -> str:
    def paths(prefix: str) -> str:
        return " ".join(
            f'(subpath (param "{name}"))' for name in params if name.startswith(prefix)
        )

    return f"""\
(version 1)
(deny default)

; Running programs. Everything a command starts inherits these rules.
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))

; Reading: anywhere, except where credentials live.
(allow file-read*)
(deny file-read* {paths("SECRET_")})

; Writing: the project and the temporary folders...
(allow file-write* (subpath (param "ROOT")) {paths("TEMP_")})
; ...but not what would run by itself later. This comes after the line
; above on purpose: when two rules match, the later one wins.
(deny file-write* {paths("PROTECTED_")})

; The devices commands write to and check.
(allow file-write* file-ioctl
  (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty")
  (literal "/dev/stdout") (literal "/dev/stderr") (regex #"^/dev/fd/"))

; What ordinary programs ask the system: CPU count, memory size, settings,
; who the user is, shared memory for Python's multiprocessing.
(allow sysctl-read)
(allow user-preference-read)
(allow ipc-posix-sem)
(allow ipc-posix-shm)
(allow mach-lookup
  (global-name "com.apple.system.opendirectoryd.libinfo")
  (global-name "com.apple.system.opendirectoryd.membership")
  (global-name "com.apple.system.logger")
  (global-name "com.apple.logd")
  (global-name "com.apple.bsd.dirhelper")
  (global-name "com.apple.system.notification_center")
  (global-name "com.apple.PowerManagement.control"))

; The network: nothing above allows it, so "deny default" covers it.
"""
