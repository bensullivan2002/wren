"""Entry point: parse flags, build the pieces, wire them together, start the REPL.

This file is deliberately boring. It's the one place that knows about every
part of Wren, so the parts themselves don't have to know about each other:

    Config ──> Provider ──> Agent ──> Repl
"""

from __future__ import annotations

import argparse
import os
import sys

from wren import __version__
from wren.agent import Agent
from wren.config import Config
from wren.prompts import build_system_prompt
from wren.providers import PROVIDERS, make_provider
from wren.repl import Repl


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wren",
        description="A small, readable coding agent for your terminal.",
    )
    parser.add_argument("--provider", choices=PROVIDERS, help="model provider (env: WREN_PROVIDER)")
    parser.add_argument("--model", help="model name, e.g. claude-opus-5-5 (env: WREN_MODEL)")
    parser.add_argument("--max-tokens", type=int, help="longest reply allowed (env: WREN_MAX_TOKENS)")
    parser.add_argument("--version", action="version", version=f"wren {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = Config.load(provider=args.provider, model=args.model, max_tokens=args.max_tokens)

    if config.provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "wren: ANTHROPIC_API_KEY is not set.\n"
            "  Create an API key in the Claude Console, then: export ANTHROPIC_API_KEY=...\n"
            "  Or try Wren offline with a pretend model:    wren --provider fake",
            file=sys.stderr,
        )
        return 1

    provider = make_provider(config.provider, config.model)
    agent = Agent(provider, build_system_prompt(), config.max_tokens)
    Repl(agent).run()
    return 0
