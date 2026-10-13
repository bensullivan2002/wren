"""Model providers, and a factory that picks one by name."""

from __future__ import annotations

from wren.config import API_KEY, CHATGPT
from wren.providers.base import Provider

PROVIDERS = ("anthropic", "openai", "fake")


def make_provider(
    name: str, model: str, effort: str | None = None, auth: str = API_KEY
) -> Provider:
    """`auth` is how OpenAI is paid for (config.AUTH_ROUTES); the other
    providers have one way only, and ignore it."""
    # Imports happen inside the branches so that, for example, the fake
    # provider works even if a real provider's SDK has a problem.
    if name == "anthropic":
        from wren.providers.anthropic import AnthropicProvider

        return AnthropicProvider(model, effort=effort)
    if name == "openai":
        from wren.providers.openai import OpenAIProvider

        if auth == CHATGPT:
            from wren.auth.chatgpt import ChatGPTSession

            plan = ChatGPTSession()
            # Not signed in, or plan usage not allowed: say so now, with
            # what to do, rather than on your first message.
            plan.check()
            return OpenAIProvider(model, effort=effort, plan=plan)
        return OpenAIProvider(model, effort=effort)
    if name == "fake":
        from wren.providers.fake import FakeProvider

        return FakeProvider(delay=0.03, effort=effort)
    raise ValueError(f"unknown provider {name!r} (choose from: {', '.join(PROVIDERS)})")


__all__ = ["PROVIDERS", "Provider", "make_provider"]
