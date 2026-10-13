"""Discovery and prompting for steering and memory; /remember's write target."""

from datetime import date
from pathlib import Path

import pytest

from wren.agent import Agent
from wren.commands import run_command
from wren.memory import MAX_FILE_CHARS, MEMORY_HEADER, STEERING_HEADER, Memory
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


def test_no_files_adds_no_sections_but_still_explains_where_to_remember(repo, home):
    memory = Memory(repo, home)
    assert memory.files == memory.steering_files == []
    assert memory.render() == ""
    prompt = build_system_prompt(repo, memory=memory.render())
    assert "<memory>" not in prompt and "<steering>" not in prompt
    assert "only to MEMORY.md in the working directory" in prompt
    assert "Do not save\nnotes unprompted" in prompt


@pytest.mark.parametrize(
    "name,tag,label",
    [
        ("WREN.md", "steering", "Steering"),
        ("AGENTS.md", "steering", "Steering"),
        ("MEMORY.md", "memory", "Memory"),
    ],
)
def test_each_kind_loads_and_is_announced_without_the_other(
    repo, home, name, tag, label
):
    (repo / name).write_text("Loaded alone.\n")
    memory = Memory(repo, home)
    assert memory.render().startswith(f"<{tag}>")
    other = "memory" if tag == "steering" else "steering"
    assert f"<{other}>" not in memory.render()
    assert memory.announcements() == [
        f"{label} loaded from: {name}  (/memory for details)"
    ]


def test_both_kinds_are_found_from_most_general_to_most_specific(repo, home):
    (home / ".wren").mkdir()
    (home / ".wren" / "WREN.md").write_text("Be brief.\n")
    # Personal memory is intentionally not part of this feature.
    (home / ".wren" / "MEMORY.md").write_text("Not loaded.\n")
    cwd = repo / "services" / "api"
    for folder in (repo, repo / "services", cwd):
        (folder / "WREN.md").write_text(f"Instructions for {folder.name}.\n")
        (folder / "MEMORY.md").write_text(f"Facts about {folder.name}.\n")
    memory = Memory(cwd, home)
    assert [file.path for file in memory.steering_files] == [
        home / ".wren" / "WREN.md",
        repo / "WREN.md",
        repo / "services" / "WREN.md",
        cwd / "WREN.md",
    ]
    assert [file.personal for file in memory.steering_files] == [
        True,
        False,
        False,
        False,
    ]
    assert [file.path for file in memory.files] == [
        repo / "MEMORY.md",
        repo / "services" / "MEMORY.md",
        cwd / "MEMORY.md",
    ]
    assert all(not file.personal for file in memory.files)
    assert memory.shown(memory.steering_files[0].path) == "~/.wren/WREN.md"
    assert memory.shown(memory.files[-1].path) == "MEMORY.md"


def test_wren_shadows_agents_but_neither_shadows_memory(repo, home):
    (repo / "AGENTS.md").write_text("From the standard file.\n")
    cwd = repo / "services" / "api"
    (cwd / "AGENTS.md").write_text("Shadowed.\n")
    (cwd / "WREN.md").write_text("Wren's own.\n")
    (repo / "MEMORY.md").write_text("Root facts.\n")
    (cwd / "MEMORY.md").write_text("Service facts.\n")
    memory = Memory(cwd, home)
    assert [file.text for file in memory.steering_files] == [
        "From the standard file.\n",
        "Wren's own.\n",
    ]
    assert [file.text for file in memory.files] == ["Root facts.\n", "Service facts.\n"]


@pytest.mark.parametrize(
    "name,collection", [("WREN.md", "steering_files"), ("MEMORY.md", "files")]
)
def test_search_stops_at_git_root(repo, home, name, collection):
    (repo.parent / name).write_text("Stray file above the repository.\n")
    (repo / name).write_text("The project's.\n")
    memory = Memory(repo / "services", home)
    assert [file.text for file in getattr(memory, collection)] == ["The project's.\n"]


@pytest.mark.parametrize(
    "name,collection", [("AGENTS.md", "steering_files"), ("MEMORY.md", "files")]
)
def test_outside_repository_only_working_directory_counts(
    tmp_path, home, name, collection
):
    (tmp_path / name).write_text("Above.\n")
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / name).write_text("Here.\n")
    assert [file.text for file in getattr(Memory(cwd, home), collection)] == ["Here.\n"]


def test_git_worktree_file_is_also_a_repository_boundary(repo, home):
    (repo / ".git").rmdir()
    (repo / ".git").write_text("gitdir: elsewhere\n")
    (repo.parent / "MEMORY.md").write_text("Outside.\n")
    (repo / "MEMORY.md").write_text("Inside.\n")
    assert [file.text for file in Memory(repo / "services", home).files] == [
        "Inside.\n"
    ]


@pytest.mark.parametrize(
    "name,collection", [("WREN.md", "steering_files"), ("MEMORY.md", "files")]
)
def test_empty_skipped_and_long_truncated(repo, home, name, collection):
    (repo / name).write_text("   \n")
    assert getattr(Memory(repo, home), collection) == []
    (repo / name).write_text("x" * (MAX_FILE_CHARS + 500))
    memory = Memory(repo, home)
    (file,) = getattr(memory, collection)
    assert file.truncated and "only its first 20,000 characters" in file.text
    assert f"{name}  (2 lines, cut off at 20,000 characters)" in memory.describe()


@pytest.mark.parametrize("name", ["WREN.md", "MEMORY.md"])
def test_unreadable_file_does_not_prevent_other_files_loading(
    repo, home, monkeypatch, name
):
    for filename in ("WREN.md", "MEMORY.md"):
        (repo / filename).write_text("Readable unless mocked.\n")
    read = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == repo / name:
            raise PermissionError("unreadable")
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    memory = Memory(repo, home)
    assert len(memory.files) + len(memory.steering_files) == 1
    assert all(file.path.name != name for file in memory.files + memory.steering_files)


def test_prompt_has_separate_sections_with_scope_precedence_and_permission_limits(
    repo, home
):
    (home / ".wren").mkdir()
    (home / ".wren" / "WREN.md").write_text("Be brief.\n")
    (repo / "WREN.md").write_text("Run make check.\n")
    (repo / "MEMORY.md").write_text("Tests live in spec/.\n")
    memory = Memory(repo, home)
    prompt = build_system_prompt(repo, today=date(2026, 10, 12), memory=memory.render())
    assert prompt.startswith(IDENTITY)
    assert (
        prompt.index(IDENTITY)
        < prompt.index("<steering>")
        < prompt.index("<memory>")
        < prompt.index("<environment>")
    )
    steering = prompt.split("<steering>")[1].split("</steering>")[0]
    notes = prompt.split("<memory>")[1].split("</memory>")[0]
    assert STEERING_HEADER in steering and MEMORY_HEADER in notes
    assert "cannot approve anything" in steering and "cannot approve anything" in notes
    assert "Steering takes precedence" in steering and "may be stale" in notes
    assert (
        '<file path="~/.wren/WREN.md" scope="personal, applies to every project">\nBe brief.\n</file>'
        in steering
    )
    assert '<file path="WREN.md" scope="project">\nRun make check.\n</file>' in steering
    assert "Tests live in spec/." not in steering
    assert (
        '<file path="MEMORY.md" scope="project">\nTests live in spec/.\n</file>'
        in notes
    )
    assert "Run make check." not in notes
    assert steering.index("Be brief.") < steering.index("Run make check.")


@pytest.mark.parametrize("name", ["WREN.md", "MEMORY.md"])
def test_prompt_rebuilt_on_clear_and_not_before(repo, home, name):
    memory = Memory(repo, home)

    def prompt():
        memory.load()
        return build_system_prompt(repo, memory=memory.render())

    agent = Agent(FakeProvider(["ok"]), prompt, 100)
    (repo / name).write_text("Added mid-session.\n")
    list(agent.run_turn("hi"))
    assert "Added mid-session." not in agent.system_prompt
    agent.reset()
    assert "Added mid-session." in agent.system_prompt
    (repo / name).unlink()
    agent.reset()
    assert "Added mid-session." not in agent.system_prompt


@pytest.mark.parametrize("name", ["WREN.md", "MEMORY.md"])
def test_restoring_history_reloads_files_without_losing_history_or_totals(
    repo, home, name
):
    memory = Memory(repo, home)

    def prompt():
        memory.load()
        return build_system_prompt(repo, memory=memory.render())

    agent = Agent(FakeProvider(["ok"]), prompt, 100)
    list(agent.run_turn("hi"))
    messages, usage, tokens = list(agent.messages), agent.usage, agent.context_tokens
    (repo / name).write_text("Changed before resume.\n")
    assert "Changed before resume." not in agent.system_prompt
    agent.restore(messages, usage, tokens)
    assert "Changed before resume." in agent.system_prompt
    assert agent.messages == messages and agent.usage == usage
    assert agent.context_tokens == tokens


def test_compaction_preserves_both_sections(repo, home):
    (repo / "WREN.md").write_text("Run make check.\n")
    (repo / "MEMORY.md").write_text("Tests live in spec/.\n")
    memory = Memory(repo, home)
    agent = Agent(
        FakeProvider(["ok", "summary"]),
        build_system_prompt(repo, memory=memory.render()),
        100,
    )
    list(agent.run_turn("hi"))
    before = agent.system_prompt
    list(agent.compact())
    assert agent.system_prompt == before
    assert "Run make check." in before and "Tests live in spec/." in before


def test_remember_creates_memory_in_working_directory_and_preserves_all_steering(
    repo, home
):
    (home / ".wren").mkdir()
    paths = [home / ".wren" / "WREN.md", repo / "WREN.md", repo / "AGENTS.md"]
    for path in paths:
        path.write_bytes(b"# Instructions\r\nUntouched")
    memory = Memory(repo, home)
    path = memory.remember("tests live in  spec/\n")
    assert path == repo / "MEMORY.md"
    assert path.read_text() == "# Notes for Wren\n\n- tests live in spec/\n"
    assert all(path.read_bytes() == b"# Instructions\r\nUntouched" for path in paths)
    assert memory.files == []  # the loaded snapshot hasn't changed


def test_remember_does_not_write_ancestor_memory(repo, home):
    (repo / "MEMORY.md").write_text("Root notes.\n")
    cwd = repo / "services" / "api"
    memory = Memory(cwd, home)
    assert len(memory.files) == 1
    assert memory.remember("Service notes.") == cwd / "MEMORY.md"
    assert (repo / "MEMORY.md").read_text() == "Root notes.\n"
    assert (cwd / "MEMORY.md").read_text() == "# Notes for Wren\n\n- Service notes.\n"


@pytest.mark.parametrize(
    "text,expected",
    [
        (b"No trailing newline", b"No trailing newline\n- note\n"),
        (b"# Notes\r\n", b"# Notes\r\n- note\r\n"),
        (b"", b"- note\n"),
    ],
)
def test_remember_appends_preserving_contents_and_newline_style(
    repo, home, text, expected
):
    path = repo / "MEMORY.md"
    memory = Memory(repo, home)
    path.write_bytes(text)  # file appeared after load
    assert memory.remember("note") == path
    assert path.read_bytes() == expected


def test_commands_show_separate_loaded_groups_and_actual_write_target(repo, home):
    (repo / "AGENTS.md").write_text("Run make check.\n")
    memory = Memory(repo, home)
    agent = Agent(FakeProvider([]), "sys", 100)
    listed = run_command(agent, "/memory", memory=memory)
    assert listed.panel and "Steering:" in listed.text and "Memory:" in listed.text
    assert "AGENTS.md  (1 line)" in listed.text
    assert "No memory files are loaded." in listed.text
    assert run_command(agent, "/remember", memory=memory).is_error
    saved = run_command(agent, "/remember tests live in spec/", memory=memory)
    assert "Added to MEMORY.md" in saved.text and "next conversation" in saved.text
    assert (repo / "AGENTS.md").read_text() == "Run make check.\n"
    assert not (repo / "WREN.md").exists()  # AGENTS.md remains active
    assert "No memory files are loaded." in memory.describe()
    memory.load()
    assert "MEMORY.md  (3 lines)" in run_command(agent, "/memory", memory=memory).text


def test_remember_write_failure_is_reported(repo, home, monkeypatch):
    memory = Memory(repo, home)
    agent = Agent(FakeProvider([]), "sys", 100)

    def fail(*args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr("wren.memory.write_text", fail)
    result = run_command(agent, "/remember note", memory=memory)
    assert result.is_error and "couldn't save that" in result.text
    assert not (repo / "MEMORY.md").exists()
