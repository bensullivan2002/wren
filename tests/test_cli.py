"""End-to-end: run the real `wren` program with the fake model and typed input."""

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from wren.config import DEFAULT_MODEL, Config
from wren.prompts import build_system_prompt

SRC = Path(__file__).resolve().parents[1] / "src"


def wren(*args, stdin="", env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("WREN_", "ANTHROPIC_"))}
    env.update({"PYTHONPATH": str(SRC), "NO_COLOR": "1", **(env_extra or {})})
    return subprocess.run(
        [sys.executable, "-m", "wren", *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_chat_with_the_fake_model():
    result = wren("--provider", "fake", stdin="hello wren\n/history\n/quit\n")
    assert result.returncode == 0
    assert "You said: hello wren" in result.stdout
    assert "[1] user" in result.stdout and "[2] assistant" in result.stdout


def test_missing_api_key_gives_a_helpful_message():
    result = wren()  # defaults to the anthropic provider, with no key set
    assert result.returncode == 1
    assert "ANTHROPIC_API_KEY is not set" in result.stderr
    assert "--provider fake" in result.stderr


def test_config_precedence_flag_then_env_then_default():
    env = {"WREN_MODEL": "from-env"}
    assert Config.load(model="from-flag", env=env).model == "from-flag"
    assert Config.load(env=env).model == "from-env"
    assert Config.load(env={}).model == DEFAULT_MODEL


def test_system_prompt_describes_the_environment():
    prompt = build_system_prompt(cwd=Path("/tmp/project"), today=date(2026, 10, 11))
    assert "Working directory: /tmp/project" in prompt
    assert "Today's date: 2026-10-11" in prompt
