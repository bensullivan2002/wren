"""The defaults you've chosen, kept between sessions.

Choosing a model or an effort with Enter in the picker means "make this my
default", and a default has to outlive the process. It's kept in one small
file, next to the saved sessions and the personal memory file:

    ~/.wren/settings.json

    {"defaults": {"anthropic": {"model": "claude-opus-5-5", "effort": "xhigh"}}}

Each provider has its own entry, because a Claude model name means nothing to
OpenAI. The model and its effort are kept together for the same reason: an
effort level belongs to the model it was chosen for. `"effort": null` is a
real value: "this model's own default effort".

Reading is forgiving, since this file is a convenience and not worth refusing
to start over: a missing or damaged file just means "nothing saved". Writing
reports its failure instead, so the app can say so.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class UserDefaults:
    def __init__(self, path: Path | None = None) -> None:
        self.path = (
            path if path is not None else Path.home() / ".wren" / "settings.json"
        )

    def get(self, provider: str) -> tuple[str, str | None] | None:
        """(model, effort) saved for this provider, or None if nothing is saved."""
        defaults = self._read().get("defaults")
        entry = defaults.get(provider) if isinstance(defaults, dict) else None
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("model"), str)
            or not entry["model"]
        ):
            return None
        effort = entry.get("effort")
        return entry["model"], effort if isinstance(effort, str) and effort else None

    def save(self, provider: str, model: str, effort: str | None) -> None:
        """Make (model, effort) this provider's default. Raises OSError if it can't be written."""
        data = self._read()
        defaults = data.get("defaults")
        if not isinstance(defaults, dict):
            defaults = data["defaults"] = {}
        defaults[provider] = {"model": model, "effort": effort}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Written to a side file and renamed into place, so a crash half-way
        # through never leaves a broken settings file behind.
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}
