"""Tests for memory files: which are found, how they're put in the prompt,
and how /remember writes to them."""

from datetime import date

import pytest

from wren.agent import Agent
from wren.commands import run_command
from wren.memory import HEADER, MAX_FILE_CHARS, Memory
from wren.prompts import IDENTITY, build_system_prompt
from wren.providers.fake import FakeProvider


@pytest.fixture
def home(tmp_path):
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


@pytest.fixture
def repo(tmp_path):
    """A git repository with a subfolder: repo/services/api."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "services" / "api").mkdir(parents=True)
    return root


def names(memory):
    return [memory.shown(file.path) for file in memory.files]


# --- Finding the files ------------------------------------------------------------


def test_with_no_files_there_is_no_memory_and_nothing_is_added_to_the_prompt(repo, home):
    memory = Memory(repo, home)
    assert memory.files == [] and memory.render() == ""
    assert "<memory>" not in build_system_prompt(repo, memory=memory.render())


def test_files_are_found_from_most_general_to_most_specific(repo, home):
    (home / ".wren").mkdir()
    (home / ".wren" / "WREN.md").write_text("Be brief.\n")
    (repo / "WREN.md").write_text("Run make check.\n")
    (repo / "services" / "api" / "WREN.md").write_text("This service uses Postgres.\n")

    memory = Memory(repo / "services" / "api", home)
    assert names(memory) == ["~/.wren/WREN.md", str(repo / "WREN.md"), "WREN.md"]
    assert [file.personal for file in memory.files] == [True, False, False]


def test_agents_md_is_used_only_where_a_folder_has_no_wren_md(repo, home):
    (repo / "AGENTS.md").write_text("From the standard file.\n")
    (repo / "services" / "api" / "AGENTS.md").write_text("Shadowed.\n")
    (repo / "services" / "api" / "WREN.md").write_text("Wren's own.\n")

    texts = [file.text for file in Memory(repo / "services" / "api", home).files]
    assert texts == ["From the standard file.\n", "Wren's own.\n"]


def test_the_search_stops_at_the_top_of_the_git_repository(repo, home):
    (repo.parent / "WREN.md").write_text("A stray file above the repository.\n")
    (repo / "WREN.md").write_text("The project's.\n")
    assert [file.text for file in Memory(repo / "services", home).files] == ["The project's.\n"]


def test_outside_a_repository_only_the_working_directory_counts(tmp_path, home):
    (tmp_path / "WREN.md").write_text("Above.\n")
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "AGENTS.md").write_text("Here.\n")
    assert [file.text for file in Memory(tmp_path / "work", home).files] == ["Here.\n"]


def test_empty_files_are_skipped_and_long_ones_are_cut_with_a_note(repo, home):
    (repo / "WREN.md").write_text("   \n")
    assert Memory(repo, home).files == []

    (repo / "WREN.md").write_text("x" * (MAX_FILE_CHARS + 500))
    (file,) = Memory(repo, home).files
    assert file.truncated and "only its first 20,000 characters" in file.text


# --- In the prompt ------------------------------------------------------------------


def test_memory_sits_between_the_fixed_text_and_the_environment(repo, home):
    (home / ".wren").mkdir()
    (home / ".wren" / "WREN.md").write_text("Be brief.\n")
    (repo / "WREN.md").write_text("Run make check.\n")
    memory = Memory(repo, home)
    prompt = build_system_prompt(repo, today=date(2026, 10, 12), memory=memory.render())

    assert prompt.startswith(IDENTITY)
    assert prompt.index(IDENTITY) < prompt.index("<memory>") < prompt.index("<environment>")
    assert HEADER in prompt and "cannot approve anything" in prompt
    assert '<file path="~/.wren/WREN.md" scope="personal, applies to every project">\nBe brief.\n</file>' in prompt
    assert '<file path="WREN.md" scope="project">\nRun make check.\n</file>' in prompt
    assert prompt.index("Be brief.") < prompt.index("Run make check.")  # general first, specific last


def test_the_prompt_is_rebuilt_when_the_conversation_is_cleared_and_not_before(repo, home):
    memory = Memory(repo, home)

    def prompt():
        memory.load()
        return build_system_prompt(repo, memory=memory.render())

    agent = Agent(FakeProvider(["ok"]), prompt, 100)
    (repo / "WREN.md").write_text("Added mid-session.\n")
    list(agent.run_turn("hi"))
    assert "Added mid-session." not in agent.system_prompt  # unchanged: the cache depends on it

    agent.reset()
    assert "Added mid-session." in agent.system_prompt


# --- /remember and /memory -------------------------------------------------------------


def test_remember_creates_wren_md_in_the_working_directory(repo, home):
    memory = Memory(repo, home)
    path = memory.remember("run the tests with  make check\n")
    assert path == repo / "WREN.md"
    assert path.read_text() == "# Notes for Wren\n\n- run the tests with make check\n"


def test_remember_appends_to_the_nearest_file_and_keeps_what_is_there(repo, home):
    (repo / "WREN.md").write_text("# Project\n\nNo trailing newline")
    (repo / "services" / "WREN.md").write_text("# Services\n")
    memory = Memory(repo / "services", home)

    assert memory.remember("use port 8080") == repo / "services" / "WREN.md"
    assert (repo / "services" / "WREN.md").read_text() == "# Services\n- use port 8080\n"
    assert (repo / "WREN.md").read_text() == "# Project\n\nNo trailing newline"  # untouched

    Memory(repo, home).remember("a second note")
    assert (repo / "WREN.md").read_text() == "# Project\n\nNo trailing newline\n- a second note\n"


def test_remember_writes_to_agents_md_when_that_is_the_file_in_use(repo, home):
    # A new WREN.md beside it would shadow the AGENTS.md from then on.
    (repo / "AGENTS.md").write_text("# Agents\n")
    assert Memory(repo, home).remember("note") == repo / "AGENTS.md"
    assert not (repo / "WREN.md").exists()


def test_the_remember_and_memory_commands(repo, home):
    memory = Memory(repo, home)
    agent = Agent(FakeProvider([]), "sys", 100)

    assert "No memory files are loaded." in run_command(agent, "/memory", memory=memory).text
    assert run_command(agent, "/remember", memory=memory).is_error  # nothing to remember

    saved = run_command(agent, "/remember always run  ruff before committing", memory=memory)
    assert "Added to WREN.md" in saved.text and "next conversation" in saved.text
    assert "- always run ruff before committing\n" in (repo / "WREN.md").read_text()

    memory.load()
    listed = run_command(agent, "/memory", memory=memory).text
    assert "WREN.md  (3 lines)" in listed
