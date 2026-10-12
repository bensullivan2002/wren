"""End-to-end: run the real `wren` program with the fake model and typed input."""

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from wren.config import DEFAULT_MODEL, Config
from wren.prompts import build_system_prompt

SRC = Path(__file__).resolve().parents[1] / "src"


def wren(*args, stdin="", cwd=None, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("WREN_", "ANTHROPIC_"))}
    env.update({"PYTHONPATH": str(SRC), "NO_COLOR": "1", **(env_extra or {})})
    return subprocess.run(
        [sys.executable, "-m", "wren", *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=30,
    )


def test_chat_with_the_fake_model():
    result = wren("--provider", "fake", stdin="hello wren\n/history\n/quit\n")
    assert result.returncode == 0
    assert "You said: hello wren" in result.stdout
    assert "[1] user" in result.stdout and "[2] assistant" in result.stdout


def test_the_fake_model_drives_a_real_tool_call(tmp_path):
    (tmp_path / "hello.txt").write_text("first line\nsecond line\n")
    result = wren("--provider", "fake", stdin="read hello.txt\n", cwd=tmp_path)

    assert "▸ read_file(path='hello.txt')  → 2 lines" in result.stdout
    assert "The first line is:      1\tfirst line" in result.stdout
    assert "2 model calls" in result.stdout


def test_tool_errors_are_shown_and_the_conversation_carries_on(tmp_path):
    result = wren("--provider", "fake", stdin="read nope.txt\nhello\n", cwd=tmp_path)
    assert "✗ No such file: nope.txt" in result.stdout
    assert "You said: hello" in result.stdout


def test_an_edit_is_shown_as_a_diff_and_happens_once_approved(tmp_path):
    (tmp_path / "hello.txt").write_text("first line\nsecond line\n")
    result = wren("--provider", "fake", stdin="edit hello.txt first => 1st\ny\n", cwd=tmp_path)

    assert "Edit hello.txt" in result.stdout
    assert "│ -first line" in result.stdout and "│ +1st line" in result.stdout
    assert "Allow? [y]es / [n]o / [a]lways allow all file edits this session" in result.stdout
    assert "→ hello.txt +1 −1" in result.stdout
    assert (tmp_path / "hello.txt").read_text() == "1st line\nsecond line\n"


def test_a_declined_command_does_not_run(tmp_path):
    result = wren("--provider", "fake", stdin="run touch made.txt\nn\n", cwd=tmp_path)
    assert "Run a shell command" in result.stdout and "│ touch made.txt" in result.stdout
    assert "✗ declined" in result.stdout
    assert not (tmp_path / "made.txt").exists()


def test_always_is_remembered_for_the_session_and_listed(tmp_path):
    stdin = "write a.txt one\na\nwrite b.txt two\n/permissions\n"
    result = wren("--provider", "fake", stdin=stdin, cwd=tmp_path)
    assert result.stdout.count("Allow?") == 1  # the second write didn't ask
    assert (tmp_path / "a.txt").read_text() == "one\n" and (tmp_path / "b.txt").read_text() == "two\n"
    assert "Allowed without asking, until you quit:\n  all file edits" in result.stdout


def test_running_out_of_input_at_a_permission_prompt_means_no(tmp_path):
    result = wren("--provider", "fake", stdin="write a.txt one\n", cwd=tmp_path)
    assert "✗ declined" in result.stdout
    assert not (tmp_path / "a.txt").exists()


def test_tools_command_lists_and_shows_tools():
    result = wren("--provider", "fake", stdin="/tools\n/tools read_file\n")
    assert "list_files" in result.stdout and "search" in result.stdout
    assert '"input_schema": {' in result.stdout


def test_missing_api_key_gives_a_helpful_message():
    result = wren()  # defaults to the anthropic provider, with no key set
    assert result.returncode == 1
    assert "ANTHROPIC_API_KEY is not set" in result.stderr
    assert "--provider fake" in result.stderr


def test_config_precedence_flag_then_env_then_default():
    env = {"WREN_MODEL": "from-env", "WREN_MAX_STEPS": "7"}
    assert Config.load(model="from-flag", env=env).model == "from-flag"
    assert Config.load(env=env).model == "from-env"
    assert Config.load(env=env).max_steps == 7
    assert Config.load(env={}).model == DEFAULT_MODEL


def test_system_prompt_describes_the_environment():
    prompt = build_system_prompt(cwd=Path("/tmp/project"), today=date(2026, 10, 11))
    assert "Working directory: /tmp/project" in prompt
    assert "Today's date: 2026-10-11" in prompt
