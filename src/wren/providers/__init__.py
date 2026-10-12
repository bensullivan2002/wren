"""Model providers, and a factory that picks one by name."""

from __future__ import annotations

from wren.providers.base import Provider

PROVIDERS = ("anthropic", "fake")


def make_provider(name: str, model: str, effort: str | None = None) -> Provider:
    # Imports happen inside the branches so that, for example, the fake
    # provider works even if a real provider's SDK has a problem.
    if name == "anthropic":
        from wren.providers.anthropic import AnthropicProvider

        return AnthropicProvider(model, effort=effort)
    if name == "fake":
        from wren.providers.fake import FakeProvider

        return FakeProvider(delay=0.03, effort=effort)
    raise ValueError(f"unknown provider {name!r} (choose from: {', '.join(PROVIDERS)})")


__all__ = ["PROVIDERS", "Provider", "make_provider"]
