"""Entry point: parse flags, build the pieces, wire them together, start a front end.

This file is deliberately boring. It's the one place that knows about every
part of Wren, so the parts themselves don't have to know about each other:

    Config ──> Provider ───────────────┐
                                       ├──> Agent ──> a front end
    Tools + Permissions ──> Registry ──┘                │
                 ▲                                      │
                 └────── "may I?" is asked by ──────────┘

There are two front ends, and the agent can't tell them apart:

    WrenApp (tui.py)   the full-screen app: what you get in a terminal
    Repl (repl.py)     plain text in, plain text out: `--plain`, pipes, scripts
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from wren import __version__
from wren.agent import Agent
from wren.config import API_KEYS, Config
from wren.commands import resume_session
from wren.memory import Memory
from wren.prompts import build_system_prompt
from wren.providers import PROVIDERS, make_provider
from wren.repl import Repl
from wren.sessions import SessionError, SessionStore
from wren.settings import UserDefaults
from wren.tools import Permissions, ToolRegistry, default_tools


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wren",
        description="A small, readable coding agent for your terminal.",
    )
    parser.add_argument("--provider", choices=PROVIDERS, help="model provider (env: WREN_PROVIDER)")
    parser.add_argument("--model", help="model name, e.g. claude-opus-5-5 (env: WREN_MODEL)")
    parser.add_argument("--max-tokens", type=int, help="longest reply allowed (env: WREN_MAX_TOKENS)")
    parser.add_argument(
        "--max-steps", type=int, help="most model calls in one turn; 0 or unset = no limit (env: WREN_MAX_STEPS)"
    )
    parser.add_argument(
        "--effort",
        help="how hard the model works on each reply, e.g. low, medium, high; "
        "what's accepted depends on the model (env: WREN_EFFORT)",
    )
    parser.add_argument(
        "--context-budget",
        type=int,
        help="compact the conversation by this many tokens at the latest (by default, Wren compacts just short "
        "of the model's context window); 0 = never compact automatically (env: WREN_CONTEXT_BUDGET)",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="",
        metavar="ID",
        help="choose an earlier conversation in this folder to reopen, or reopen the one with this id",
    )
    parser.add_argument(
        "--plain", action="store_true", help="use the plain line-by-line interface, not the full-screen app"
    )
    parser.add_argument("--version", action="version", version=f"wren {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    defaults = UserDefaults()  # ~/.wren/settings.json: what was chosen with Enter in the pickers
    config = Config.load(
        user=defaults,
        provider=args.provider,
        model=args.model,
        max_tokens=args.max_tokens,
        max_steps=args.max_steps,
        context_budget=args.context_budget,
        effort=args.effort,
    )

    key = API_KEYS.get(config.provider)
    if key is not None and not os.environ.get(key[0]):
        variable, where = key
        print(
            f"wren: {variable} is not set.\n"
            f"  Create an API key in {where}, then: export {variable}=...\n"
            "  Or try Wren offline with a pretend model:    wren --provider fake",
            file=sys.stderr,
        )
        return 1

    root = Path.cwd()
    provider = make_provider(config.provider, config.model, config.effort)
    permissions = Permissions()
    tools = ToolRegistry(default_tools(), root=root, permissions=permissions)
    memory = Memory(root)

    def system_prompt() -> str:
        # Called now, and again whenever the conversation is cleared, so a
        # memory file edited mid-session is picked up by the next conversation.
        memory.load()
        return build_system_prompt(root, memory=memory.render())

    agent = Agent(
        provider, system_prompt, config.max_tokens, tools, config.max_steps, context_cap=config.context_budget
    )

    # From here on, every message is written to a session file as it's added.
    sessions = SessionStore(root, provider.name, provider.model, effort=provider.effort)
    agent.journal = sessions
    notice = ""
    # `--resume` alone opens the same list that /resume shows, and the front
    # end does that once it's running. `--resume ID` needs no choosing.
    choose_session = args.resume == ""
    if args.resume:
        try:
            notice = resume_session(agent, sessions, sessions.find(args.resume))
        except (SessionError, ValueError) as e:
            notice = f"Couldn't resume: {e}. This is a new conversation."

    # A full-screen app needs a real terminal at both ends. When input or
    # output is a pipe or a file (`echo hi | wren`, the tests), fall back to
    # the plain REPL, which only needs lines of text.
    if args.plain or not (sys.stdin.isatty() and sys.stdout.isatty()):
        repl = Repl(agent, memory=memory, sessions=sessions, notice=notice, choose_session=choose_session, defaults=defaults)
        # The one loop in the wiring: tools need to ask the user, and the
        # front end is the part that talks to the user. Until this line,
        # Permissions has nobody to ask, and so refuses everything.
        permissions.ask = repl.ask_permission
        repl.run()
    else:
        # Imported here so that plain mode never loads Textual at all.
        from wren.exchange import ExchangeRate
        from wren.tui import WrenApp

        # Wires itself to the same hooks in its __init__.
        WrenApp(agent, memory, sessions, notice, choose_session, defaults, exchange=ExchangeRate()).run()
    return 0
