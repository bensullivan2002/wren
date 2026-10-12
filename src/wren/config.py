"""Settings, and where they come from.

Each setting is looked up in layers, and the first layer that has a value wins:

    1. a command-line flag     wren --model claude-opus-5-5
    2. an environment variable WREN_MODEL=claude-opus-5-5 wren
    3. your saved default      chosen with Enter in the /model or /effort picker
    4. Wren's own default      below
    5. the model's own default (for effort: nothing is sent)

Flags win because they're the most specific ("just this run"); environment
variables are good for "always, on this machine" (put them in your shell
profile); your saved default (settings.py) is what you last chose to keep.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from wren.settings import UserDefaults

DEFAULT_PROVIDER = "anthropic"
# The model and effort each provider starts with: the strongest model, at
# extra-high effort. /model and /effort change it for a session, and choosing with
# Enter in their pickers changes it for good (settings.py).
DEFAULT_MODELS = {
    "anthropic": "claude-opus-5-5",
    "openai": "gpt-6.1-sol",
    "fake": "demo",
}
DEFAULT_EFFORTS: dict[str, str | None] = {
    "anthropic": "xhigh",
    "openai": "xhigh",
    "fake": None,
}
DEFAULT_MODEL = DEFAULT_MODELS[DEFAULT_PROVIDER]


def default_choice(provider: str, user: UserDefaults | None = None) -> tuple[str, str | None]:
    """The (model, effort) a provider starts with when nothing was asked for:
    what you saved, else Wren's own. An effort of None means "the model's own"."""
    saved = user.get(provider) if user is not None else None
    if saved is not None:
        return saved
    return DEFAULT_MODELS.get(provider, DEFAULT_MODEL), DEFAULT_EFFORTS.get(provider)


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
        user: UserDefaults | None = None,
    ) -> Config:
        """Build a Config. Arguments are command-line flags (None = not given);
        `user` is where the saved defaults are read from."""
        provider = provider or env.get("WREN_PROVIDER") or DEFAULT_PROVIDER
        # The defaults depend on the provider: a Claude model name means
        # nothing to OpenAI.
        default_model, default_effort = default_choice(provider, user)
        model = model or env.get("WREN_MODEL") or default_model
        # A default effort belongs to its model. Starting on some other model
        # (a flag, or WREN_MODEL) must not borrow it: that level might not
        # exist there. That model starts at its own default.
        effort = effort or env.get("WREN_EFFORT") or (default_effort if model == default_model else None)
        return cls(
            provider=provider,
            model=model,
            max_tokens=max_tokens or int(env.get("WREN_MAX_TOKENS") or DEFAULT_MAX_TOKENS),
            max_steps=max_steps or int(env.get("WREN_MAX_STEPS") or DEFAULT_MAX_STEPS),
            effort=effort,
            # `is not None`, not `or`: 0 is a real choice here (never compact).
            context_budget=(
                context_budget
                if context_budget is not None
                else int(env.get("WREN_CONTEXT_BUDGET") or DEFAULT_CONTEXT_BUDGET)
            ),
        )
