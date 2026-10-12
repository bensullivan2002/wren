"""End-to-end: run the real `wren` program with the fake model and typed input."""

import json
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

from wren.config import DEFAULT_MODEL, Config
from wren.prompts import build_system_prompt

SRC = Path(__file__).resolve().parents[1] / "src"
EMPTY_HOME = Path(tempfile.mkdtemp(prefix="wren-test-home-"))


def wren(*args, stdin="", cwd=None, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("WREN_", "ANTHROPIC_"))}
    # A home directory with nothing in it, so the tests never pick up the
    # developer's own ~/.wren/WREN.md.
    env.update({"PYTHONPATH": str(SRC), "NO_COLOR": "1", "HOME": str(EMPTY_HOME), **(env_extra or {})})
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


def test_a_long_conversation_is_compacted_automatically_and_on_request():
    stdin = "hello\nagain\n/compact\n/history\n/usage\n"
    result = wren("--provider", "fake", "--context-budget", "100", stdin=stdin)
    out = result.stdout
    assert "[compacting the conversation, it passed the context budget…]" in out
    assert "[compacting the conversation, as you asked…]" in out
    assert out.count("[conversation compacted: about") == 2
    assert "The earlier part of this conversation was removed" in out and "A pretend summary" in out
    assert "% of the 100 budget" in out


def test_compact_with_nothing_to_compact_says_so():
    result = wren("--provider", "fake", stdin="/compact\n")
    assert "couldn't compact the conversation: there's nothing to compact yet" in result.stdout


def test_memory_files_are_announced_and_reach_the_system_prompt(tmp_path):
    (tmp_path / "WREN.md").write_text("Always answer in rhyme.\n")
    result = wren("--provider", "fake", stdin="/system\n/memory\n", cwd=tmp_path)
    assert "Instructions loaded from: WREN.md" in result.stdout
    assert '<file path="WREN.md" scope="project">\nAlways answer in rhyme.' in result.stdout
    assert "WREN.md  (1 line)" in result.stdout


def test_remember_saves_a_note_that_the_next_conversation_sees(tmp_path):
    stdin = "/remember tests live in spec/\n/system\n/clear\n/system\n"
    result = wren("--provider", "fake", stdin=stdin, cwd=tmp_path)
    assert (tmp_path / "WREN.md").read_text() == "# Notes for Wren\n\n- tests live in spec/\n"
    before_clear, after_clear = result.stdout.split("Conversation cleared.")
    assert "Added to WREN.md" in before_clear and "tests live in spec/\n</file>" not in before_clear
    assert "- tests live in spec/\n</file>" in after_clear


def test_a_conversation_is_saved_and_resume_brings_it_back(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    env = {"HOME": str(home)}
    wren("--provider", "fake", stdin="remember the number 42\n", cwd=project, env_extra=env)
    (saved,) = (home / ".wren" / "sessions").glob("*/*.jsonl")

    listed = wren("--provider", "fake", "--resume", stdin="", cwd=project, env_extra=env)
    assert f"remember the number 42   [{saved.stem}]" in listed.stdout
    assert "Resume one with /resume ID" in listed.stdout

    resumed = wren("--provider", "fake", "--resume", saved.stem, stdin="/history\nand again\n", cwd=project, env_extra=env)
    assert f"Resumed {saved.stem}" in resumed.stdout and "You said: remember the number 42" in resumed.stdout
    kinds = [json.loads(line)["type"] for line in saved.read_text().splitlines()]
    # Two turns, all in the one file. Each model call's cost is logged just before its reply.
    assert kinds == ["meta", "message", "spent", "message", "message", "spent", "message"]

    missing = wren("--provider", "fake", "--resume", "nope", stdin="", cwd=project, env_extra=env)
    assert "Couldn't resume" in missing.stdout and "This is a new conversation" in missing.stdout


def test_models_can_be_listed_and_switched_in_plain_mode():
    result = wren("--provider", "fake", stdin="/model\n/model demo-fast\n/model\n")
    assert "demo  (in use)" in result.stdout and "Switch with /model NAME" in result.stdout
    assert "Now using demo-fast." in result.stdout and "demo-fast  (in use)" in result.stdout


def test_effort_can_be_listed_and_set_in_plain_mode_and_from_the_command_line():
    result = wren("--provider", "fake", "--effort", "low", stdin="/effort\n/effort high\n/model demo-fast\n/effort\n")
    out = result.stdout
    assert "demo (low effort)" in out  # the banner
    assert "low  (in use)" in out and "Switch with /effort LEVEL" in out
    assert "default (which is medium for this model)" in out
    assert "Effort is now high." in out
    assert "demo-fast has no separate effort setting" in out


def test_tools_command_lists_and_shows_tools():
    result = wren("--provider", "fake", stdin="/tools\n/tools read_file\n")
    assert "list_files" in result.stdout and "search" in result.stdout
    assert '"input_schema": {' in result.stdout


def test_missing_api_key_gives_a_helpful_message():
    result = wren()  # defaults to the anthropic provider, with no key set
    assert result.returncode == 1
    assert "ANTHROPIC_API_KEY is not set" in result.stderr
    assert "--provider fake" in result.stderr


def test_each_provider_names_its_own_missing_key():
    result = wren("--provider", "openai")
    assert result.returncode == 1
    assert "OPENAI_API_KEY is not set" in result.stderr and "--provider fake" in result.stderr


def test_config_precedence_flag_then_env_then_default():
    env = {"WREN_MODEL": "from-env", "WREN_MAX_STEPS": "7"}
    assert Config.load(model="from-flag", env=env).model == "from-flag"
    assert Config.load(env=env).model == "from-env"
    assert Config.load(env=env).max_steps == 7
    assert Config.load(env={}).model == DEFAULT_MODEL
    # The default model follows the provider: a Claude name means nothing to OpenAI.
    assert Config.load(provider="openai", env={}).model == "gpt-5.6-terra"
    assert Config.load(env={"WREN_PROVIDER": "openai", "WREN_MODEL": "gpt-6-astra"}).model == "gpt-6-astra"
    # 0 is a real value for the budget ("never compact"), from a flag or the environment.
    assert Config.load(context_budget=0, env={"WREN_CONTEXT_BUDGET": "5"}).context_budget == 0
    assert Config.load(env={"WREN_CONTEXT_BUDGET": "0"}).context_budget == 0
    assert Config.load(env={}).context_budget == 150_000
    assert Config.load(env={}).effort is None  # unset: the model's own default
    assert Config.load(env={"WREN_EFFORT": "low"}).effort == "low"
    assert Config.load(effort="high", env={"WREN_EFFORT": "low"}).effort == "high"


def test_system_prompt_describes_the_environment():
    prompt = build_system_prompt(cwd=Path("/tmp/project"), today=date(2026, 10, 11))
    assert "Working directory: /tmp/project" in prompt
    assert "Today's date: 2026-10-11" in prompt
