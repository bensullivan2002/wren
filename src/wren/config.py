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
# The model each provider starts with. For each, the mid-priced one: strong
# at coding, cheaper and faster than the top of the range. /model changes it.
DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5-5",
    "openai": "gpt-5.6-terra",
    "fake": "demo",
}
DEFAULT_MODEL = DEFAULT_MODELS[DEFAULT_PROVIDER]

# Where each provider's SDK looks for its key, and where to get one.
API_KEYS = {
    "anthropic": ("ANTHROPIC_API_KEY", "the Claude Console"),
    "openai": ("OPENAI_API_KEY", "the OpenAI platform dashboard"),
}
# The most the model may write in one reply, *including* its private
# thinking, which current models do before answering and which can't be
# switched off. It's a ceiling, not a cost: you pay for what's written.
DEFAULT_MAX_TOKENS = 32_000
DEFAULT_MAX_STEPS = 25  # the most model calls in one turn, before Wren stops and checks in
# How large the conversation may grow before Wren compacts it (see
# Agent.compact). The model could take far more (a million tokens); this is
# about cost, since every request re-sends the lot. 0 turns it off.
DEFAULT_CONTEXT_BUDGET = 150_000


@dataclass
class Config:
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_steps: int = DEFAULT_MAX_STEPS
    context_budget: int = DEFAULT_CONTEXT_BUDGET
    effort: str | None = None  # None: whatever the model does by default

    @classmethod
    def load(
        cls,
        *,
        provider: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        max_steps: int | None = None,
        context_budget: int | None = None,
        effort: str | None = None,
        env: Mapping[str, str] = os.environ,
    ) -> Config:
        """Build a Config. Arguments are command-line flags (None = not given)."""
        provider = provider or env.get("WREN_PROVIDER") or DEFAULT_PROVIDER
        return cls(
            provider=provider,
            # The default depends on the provider: a Claude model name means
            # nothing to OpenAI.
            model=model or env.get("WREN_MODEL") or DEFAULT_MODELS.get(provider, DEFAULT_MODEL),
            max_tokens=max_tokens or int(env.get("WREN_MAX_TOKENS") or DEFAULT_MAX_TOKENS),
            max_steps=max_steps or int(env.get("WREN_MAX_STEPS") or DEFAULT_MAX_STEPS),
            # `is not None`, not `or`: 0 is a real choice here (never compact).
            effort=effort or env.get("WREN_EFFORT") or None,
            context_budget=(
                context_budget
                if context_budget is not None
                else int(env.get("WREN_CONTEXT_BUDGET") or DEFAULT_CONTEXT_BUDGET)
            ),
        )
