"""Settings, and where they come from.

Each setting is looked up in layers, and the first layer that has a value wins:

    1. a command-line flag     wren --model claude-opus-5-5
    2. an environment variable WREN_MODEL=claude-opus-5-5 wren
    3. the default below

Flags win because they're the most specific ("just this run"); environment
variables are good for "always, on this machine" (put them in your shell
profile). A config file would slot in between 2 and 3 when we need one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-sonnet-5-5"  # strong at coding, cheaper and faster than Opus
DEFAULT_MAX_TOKENS = 8192  # the most the model may write in one reply


@dataclass
class Config:
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS

    @classmethod
    def load(
        cls,
        *,
        provider: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        env: Mapping[str, str] = os.environ,
    ) -> Config:
        """Build a Config. Arguments are command-line flags (None = not given)."""
        return cls(
            provider=provider or env.get("WREN_PROVIDER") or DEFAULT_PROVIDER,
            model=model or env.get("WREN_MODEL") or DEFAULT_MODEL,
            max_tokens=max_tokens or int(env.get("WREN_MAX_TOKENS") or DEFAULT_MAX_TOKENS),
        )
