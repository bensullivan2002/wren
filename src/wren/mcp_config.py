"""Which MCP servers to start: reading `~/.wren/mcp.json`.

An MCP server is a program that offers tools. Wren starts each one you list
here, asks what tools it has, and offers them to the model beside its own.
The file uses the same `mcpServers` shape as Claude Code and Cursor, so an
entry can be copied from a server's README or from another tool's config:

    {
      "mcpServers": {
        "github": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-github"],
          "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}
        }
      }
    }

Three things about it are deliberate:

  * It's personal only. A server entry is a program Wren will launch with
    your permissions, outside the sandbox. A `.mcp.json` inside a project
    would let any repository you clone choose programs to run, so that
    waits for a step with a "trust this project?" question (D139).
  * `${NAME}` is replaced with the environment variable NAME (or
    `${NAME:-fallback}` when it may be unset), so a secret can stay in your
    shell's environment instead of being written into this file. A server
    gets only the variables listed under `env`, plus a handful the MCP SDK
    passes on (PATH, HOME and so on): never your API keys by accident.
  * It's checked strictly. A misspelt key ("arg" for "args") would otherwise
    be ignored quietly and leave you wondering why a server misbehaves, so
    that server is skipped with a message saying why, and the others still
    start. Only remote servers (a "url") are skipped for a different
    reason: Wren doesn't speak MCP over HTTP yet (D137).

Reading never stops Wren from starting: a missing file means no servers, and
any problem becomes a line in the startup notice.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# A server's name becomes part of each of its tools' names
# (`mcp__github__create_issue`), and the providers accept only these
# characters there. Refusing other names is clearer than rewriting them.
SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# The keys a stdio server entry may have. "type" is accepted (as "stdio")
# because other tools' configs often say it.
_STDIO_KEYS = {"type", "command", "args", "env", "cwd"}


def default_path() -> Path:
    return Path.home() / ".wren" / "mcp.json"


@dataclass
class ServerConfig:
    """One server to start: a command, as you'd type it, plus its environment."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None


@dataclass
class McpConfig:
    servers: list[ServerConfig] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # one line each, for the notice
    path: Path | None = None  # where it was read from, for /mcp


def load(path: Path | None = None, environ: dict[str, str] | None = None) -> McpConfig:
    """Read the config file. Never raises: problems come back as messages."""
    path = path if path is not None else default_path()
    environ = environ if environ is not None else dict(os.environ)
    config = McpConfig(path=path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return config
    except OSError as e:
        config.problems.append(f"couldn't read {path}: {e.strerror or e}")
        return config
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        config.problems.append(f"{path} isn't valid JSON (line {e.lineno}): {e.msg}")
        return config
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        config.problems.append(
            f'{path} has no "mcpServers" object, so no servers were started'
        )
        return config
    for name, entry in servers.items():
        try:
            config.servers.append(_server(name, entry, environ))
        except ValueError as e:
            config.problems.append(f"MCP server {name!r} skipped: {e}")
    return config


def _server(name: str, entry: Any, environ: dict[str, str]) -> ServerConfig:
    if not SERVER_NAME.match(name):
        raise ValueError("names may use only letters, digits, - and _ (up to 32)")
    if not isinstance(entry, dict):
        raise ValueError("its entry should be an object")  # noqa: TRY004 - a mistake in your file, reported like the others
    if "url" in entry or entry.get("type", "stdio") != "stdio":
        raise ValueError(
            "remote (HTTP) servers aren't supported yet, only ones Wren starts itself"
        )
    unknown = sorted(set(entry) - _STDIO_KEYS)
    if unknown:
        raise ValueError(
            f"unknown key {', '.join(map(repr, unknown))} (expected command, args, env, cwd)"
        )

    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError('"command" should be the program to run, as a string')
    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ValueError('"args" should be a list of strings')
    env = entry.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        raise ValueError('"env" should be an object whose values are strings')
    cwd = entry.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError('"cwd" should be a folder, as a string')

    return ServerConfig(
        name=name,
        command=expand(command, environ),
        args=[expand(a, environ) for a in args],
        env={key: expand(value, environ) for key, value in env.items()},
        cwd=expand(cwd, environ) if cwd is not None else None,
    )


def expand(text: str, environ: dict[str, str]) -> str:
    """Replace `${NAME}` and `${NAME:-fallback}` with values from the environment.

    An unset variable with no fallback is an error, not an empty string: a
    server started with an empty token fails later in a far more confusing way.
    """

    def value(match: re.Match[str]) -> str:
        name, fallback = match.group(1), match.group(2)
        if name in environ:
            return environ[name]
        if fallback is not None:
            return fallback
        raise ValueError(
            f"${{{name}}} is used, but {name} isn't set in the environment"
        )

    return _VARIABLE.sub(value, text)
