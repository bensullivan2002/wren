"""Tests for the tool framework (inputs, registry, path safety) and the tools themselves.

Each test builds a small throwaway project in pytest's `tmp_path`.
"""

import os
import shlex
import sys

import pytest
from pydantic import Field

from wren.agent import Agent
from wren.commands import plain_call, run_command
from wren.messages import ToolUseBlock
from wren.providers.fake import FakeProvider
from wren.tools import (
    Decision,
    Permissions,
    Tool,
    ToolError,
    ToolInput,
    ToolRegistry,
    ToolResult,
    default_tools,
    sandbox,
)
from wren.tools import list_files as list_files_module
from wren.tools import search as search_module
from wren.tools import shell as shell_module
from wren.tools.files import glob_to_regex
from wren.tools.registry import DECLINED
from wren.tools.shell import command_word


@pytest.fixture
def project(tmp_path):
    (tmp_path / "README.md").write_text("# Demo\nHello world\n")
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "app" / "main.py").write_text(
        "def main():\n    print('Hello')\n"
    )
    (tmp_path / "src" / "app" / "util.py").write_text("def helper():\n    return 42\n")
    (tmp_path / "src" / "notes.txt").write_text("TODO: hello again\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("hello from git\n")
    (tmp_path / "node_modules" / "lib").mkdir(parents=True)
    (tmp_path / "node_modules" / "lib" / "index.js").write_text("hello from node\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0hello")
    return tmp_path


class Answers:
    """Stands in for the person at the permission prompt: gives a fixed
    answer, and remembers every request it was shown."""

    def __init__(self, decision=Decision.ALLOW):
        self.decision = decision
        self.seen = []

    def __call__(self, request):
        self.seen.append(request)
        return self.decision


@pytest.fixture
def user():
    return Answers()


@pytest.fixture
def tools(project, user):
    return ToolRegistry(default_tools(), root=project, permissions=Permissions(user))


def ok(result):
    assert not result.is_error, result.content
    return result


def err(result):
    assert result.is_error, result.content
    return result


# --- Tool inputs: one Pydantic model gives the schema and the checking ----------


class Demo(Tool):
    name = "demo"
    description = "A tool for testing inputs."

    class Input(ToolInput):
        """A docstring here is for people; it must not reach the model."""

        path: str = Field(description="Where to look.")
        limit: int = Field(10, ge=1)
        loud: bool = False


def test_the_schema_is_generated_from_the_input_model():
    assert Demo().spec().input_schema == {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Where to look."},
            "limit": {"type": "integer", "minimum": 1, "default": 10},
            "loud": {"type": "boolean", "default": False},
        },
        "required": ["path"],
        "additionalProperties": False,
    }


def test_valid_args_become_a_typed_object_with_defaults_filled_in():
    args = Demo().parse({"path": "a"})
    assert (args.path, args.limit, args.loud) == ("a", 10, False)
    assert Demo().parse({"path": "a", "limit": 3}).limit == 3


def test_every_problem_is_reported_at_once():
    with pytest.raises(ToolError) as e:
        Demo().parse({"limit": "ten", "colour": "red"})
    message = str(e.value)
    assert "missing required argument 'path'" in message
    assert "'limit': Input should be a valid integer (got 'ten')" in message
    assert "unknown argument 'colour'" in message
    assert "Accepted arguments: path, limit, loud" in message


@pytest.mark.parametrize("almost", [True, "3", 3.0])
def test_nothing_is_quietly_converted_to_an_integer(almost):
    with pytest.raises(ToolError, match="'limit': Input should be a valid integer"):
        Demo().parse({"path": "a", "limit": almost})


def test_minimum_is_enforced():
    with pytest.raises(ToolError, match="greater than or equal to 1"):
        Demo().parse({"path": "a", "limit": 0})


def test_arguments_must_be_an_object():
    with pytest.raises(ToolError, match="JSON object"):
        Demo().parse(["a"])


def test_a_tool_without_an_input_model_takes_no_arguments(tmp_path):
    assert Boom().spec().input_schema == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert (
        "unknown argument 'x'"
        in err(ToolRegistry([Boom()], root=tmp_path).run("boom", {"x": 1})).content
    )


def test_every_real_tool_describes_all_of_its_arguments():
    for tool in default_tools():
        for name, rules in tool.spec().input_schema["properties"].items():
            assert rules.get("description"), f"{tool.name}.{name} has no description"


# --- The registry -------------------------------------------------------------


class Boom(Tool):
    name = "boom"
    description = "Always crashes."

    def run(self, args, ctx):
        raise ZeroDivisionError("division by zero")


class Big(Tool):
    name = "big"
    description = "Returns a lot of text."

    def run(self, args, ctx):
        return ToolResult("x" * 500, summary="big")


def test_unknown_tool_is_an_error_result_listing_the_real_ones(tmp_path):
    result = err(ToolRegistry([Boom()], root=tmp_path).run("nope", {}))
    assert "Available tools: boom" in result.content


def test_a_crashing_tool_becomes_an_error_result(tmp_path):
    result = err(ToolRegistry([Boom()], root=tmp_path).run("boom", {}))
    assert "ZeroDivisionError" in result.content


def test_invalid_arguments_are_an_error_result(tools):
    assert "missing required argument 'path'" in err(tools.run("read_file", {})).content


def test_huge_output_is_truncated_with_a_note(tmp_path):
    result = ok(
        ToolRegistry([Big()], root=tmp_path, max_output_chars=100).run("big", {})
    )
    assert result.content.startswith("x" * 100)
    assert "showing the first 100 of 500 characters" in result.content


def test_duplicate_tool_names_are_refused(tmp_path):
    with pytest.raises(ValueError):
        ToolRegistry([Boom(), Boom()], root=tmp_path)


# --- Staying inside the working directory ---------------------------------------


@pytest.mark.parametrize(
    "path", ["../outside.txt", "src/../../outside.txt", "/etc/hostname"]
)
def test_paths_outside_the_working_directory_are_refused(tools, path):
    assert (
        "outside the working directory"
        in err(tools.run("read_file", {"path": path})).content
    )


def test_a_symlink_pointing_outside_is_refused(tools, project, tmp_path_factory):
    secret = tmp_path_factory.mktemp("elsewhere") / "secret.txt"
    secret.write_text("password=hunter2\n")
    os.symlink(secret, project / "innocent.txt")

    assert (
        "outside the working directory"
        in err(tools.run("read_file", {"path": "innocent.txt"})).content
    )
    assert "hunter2" not in tools.run("search", {"pattern": "password"}).content


def test_absolute_paths_inside_the_working_directory_are_fine(tools, project):
    ok(tools.run("read_file", {"path": str(project / "README.md")}))


# --- read_file ----------------------------------------------------------------


def test_read_file_numbers_lines(tools):
    result = ok(tools.run("read_file", {"path": "src/app/main.py"}))
    assert result.content == "     1\tdef main():\n     2\t    print('Hello')"
    assert result.summary == "2 lines"


def test_read_file_pages_through_long_files(tools, project):
    (project / "long.txt").write_text("".join(f"line {i}\n" for i in range(1, 11)))
    result = ok(tools.run("read_file", {"path": "long.txt", "offset": 4, "limit": 3}))
    assert result.content.splitlines()[0] == "     4\tline 4"
    assert "Showing lines 4-6 of 10" in result.content
    assert "offset=7" in result.content
    assert (
        "offset"
        in err(tools.run("read_file", {"path": "long.txt", "offset": 11})).content
    )


@pytest.mark.parametrize(
    "path, message",
    [("missing.py", "No such file"), ("src", "is a folder"), ("logo.png", "binary")],
)
def test_read_file_explains_what_went_wrong(tools, path, message):
    assert message in err(tools.run("read_file", {"path": path})).content


def test_read_file_handles_empty_files_and_long_lines(tools, project):
    (project / "empty.txt").write_text("")
    assert (
        ok(tools.run("read_file", {"path": "empty.txt"})).content
        == "(this file is empty)"
    )
    (project / "wide.txt").write_text("y" * 2000)
    assert (
        "1,500 more characters"
        in ok(tools.run("read_file", {"path": "wide.txt"})).content
    )


# --- list_files ---------------------------------------------------------------


def test_list_files_shows_one_folder_by_default(tools):
    listing = ok(tools.run("list_files", {})).content.splitlines()
    assert listing == [
        "src/",
        "README.md",
        "logo.png",
    ]  # .git and node_modules are skipped


def test_list_files_recurses_with_double_star(tools):
    listing = ok(tools.run("list_files", {"pattern": "**/*.py"})).content.splitlines()
    assert listing == ["src/app/main.py", "src/app/util.py"]


def test_list_files_paths_are_relative_to_the_working_directory(tools):
    listing = ok(tools.run("list_files", {"path": "src"})).content.splitlines()
    assert listing == ["src/app/", "src/notes.txt"]


def test_list_files_caps_long_listings(tools, project, monkeypatch):
    monkeypatch.setattr(list_files_module, "MAX_RESULTS", 2)
    result = ok(tools.run("list_files", {"pattern": "**"}))
    assert "Showing 2 of" in result.content


def test_list_files_says_when_nothing_matches(tools):
    result = ok(tools.run("list_files", {"pattern": "*.rs"}))
    assert result.summary == "no matches"


def test_list_files_rejects_a_file_as_the_folder(tools):
    assert "is a file" in err(tools.run("list_files", {"path": "README.md"})).content


@pytest.mark.parametrize(
    "pattern, path, matches",
    [
        ("*.py", "main.py", True),
        ("*.py", "src/main.py", False),  # * stays within one folder
        ("**/*.py", "main.py", True),  # **/ can match zero folders
        ("**/*.py", "a/b/c/main.py", True),
        ("src/**/test_*.py", "src/test_x.py", True),
        ("src/**/test_*.py", "lib/test_x.py", False),
        ("file?.txt", "file1.txt", True),
        ("a.b", "axb", False),  # a dot is a dot, not "any character"
    ],
)
def test_glob_to_regex(pattern, path, matches):
    assert bool(glob_to_regex(pattern).fullmatch(path)) is matches


# --- search -------------------------------------------------------------------


def test_search_finds_matching_lines_with_locations(tools):
    result = ok(tools.run("search", {"pattern": "Hello"}))
    assert result.content.splitlines() == [
        "README.md:2: Hello world",
        "src/app/main.py:2:     print('Hello')",
    ]
    assert result.summary == "2 matches in 2 files"


def test_search_skips_ignored_folders_and_binary_files(tools):
    content = ok(tools.run("search", {"pattern": "hello", "ignore_case": True})).content
    assert "git" not in content and "node" not in content and "logo" not in content
    assert "notes.txt" in content


def test_search_can_filter_by_file_name_and_path(tools):
    content = ok(tools.run("search", {"pattern": "def", "include": "util*.py"})).content
    assert content == "src/app/util.py:1: def helper():"
    single = ok(tools.run("search", {"pattern": "return", "path": "src/app/util.py"}))
    assert single.summary == "1 match in 1 file"


def test_search_reports_bad_regex_and_no_matches(tools):
    assert (
        "Invalid regular expression"
        in err(tools.run("search", {"pattern": "("})).content
    )
    assert ok(tools.run("search", {"pattern": "zebra"})).summary == "no matches"


def test_search_caps_matches(tools, monkeypatch):
    monkeypatch.setattr(search_module, "MAX_MATCHES", 1)
    result = ok(tools.run("search", {"pattern": "e"}))
    assert len(result.content.split("\n\n")[0].splitlines()) == 1
    assert "Showing the first 1 of" in result.content


# --- Permissions ---------------------------------------------------------------


def test_read_only_tools_never_ask(tools, user):
    ok(tools.run("list_files", {}))
    ok(tools.run("read_file", {"path": "README.md"}))
    ok(tools.run("search", {"pattern": "Hello"}))
    assert user.seen == []


def test_a_declined_action_does_not_happen_and_the_model_is_told(tools, user, project):
    user.decision = Decision.DENY
    result = err(tools.run("write_file", {"path": "new.txt", "content": "hi"}))
    assert result.content == DECLINED
    assert not (project / "new.txt").exists()


def test_with_nobody_to_ask_the_answer_is_no(project):
    tools = ToolRegistry(default_tools(), root=project)  # no Permissions given
    outside = {"description": WHY, "command": "echo hi", "outside_sandbox": True}
    err(tools.run("shell", outside))  # asked about everywhere, sandbox or not
    err(tools.run("write_file", {"path": "new.txt", "content": "hi"}))
    assert not (project / "new.txt").exists()


def test_always_stops_the_asking_for_that_scope_only(tools, user, monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: False)  # so commands ask
    user.decision = Decision.ALWAYS
    ok(tools.run("write_file", {"path": "a.txt", "content": "1"}))
    ok(tools.run("write_file", {"path": "b.txt", "content": "2"}))
    ok(tools.run("edit_file", {"path": "a.txt", "old_string": "1", "new_string": "3"}))
    assert len(user.seen) == 1  # asked once; edits and writes share the "edit" scope

    ok(tools.run("shell", {"description": WHY, "command": "echo one"}))
    ok(tools.run("shell", {"description": WHY, "command": "echo two"}))
    ok(tools.run("shell", {"description": WHY, "command": "true"}))
    assert [r.scope for r in user.seen] == ["edit", "shell:echo", "shell:true"]
    assert tools.permissions.always == {
        "edit": "all file edits",
        "shell:echo": "commands starting with `echo`",
        "shell:true": "commands starting with `true`",
    }


def test_always_for_a_command_does_not_cover_a_chained_command(
    tools, user, project, monkeypatch
):
    monkeypatch.setattr(sandbox, "available", lambda: False)  # so commands ask
    user.decision = Decision.ALWAYS
    ok(tools.run("shell", {"description": WHY, "command": "echo hi"}))
    user.decision = Decision.DENY
    err(
        tools.run("shell", {"description": WHY, "command": "echo hi; touch sneaky.txt"})
    )
    assert user.seen[-1].scope is None  # "always" isn't even offered for it
    assert not (project / "sneaky.txt").exists()


@pytest.mark.parametrize(
    "command, word",
    [
        ("pytest -q tests", "pytest"),
        (
            "git commit -m 'a message; with punctuation?'",
            None,
        ),  # we don't parse quotes: when unsure, ask
        ("git status", "git"),
        ("echo hi; rm -rf /", None),
        ("echo hi && rm x", None),
        ("cat a | sh", None),
        ("echo $(whoami)", None),
        ("echo hi > out.txt", None),
        ("echo one\nrm x", None),
        ("FOO=1 pytest", None),
        ("echo 'unclosed", None),
        ("   ", None),
    ],
)
def test_command_word(command, word):
    assert command_word(command) == word


def test_a_call_that_cannot_work_fails_before_the_user_is_asked(tools, user):
    err(
        tools.run(
            "edit_file",
            {"path": "README.md", "old_string": "not there", "new_string": "x"},
        )
    )
    err(tools.run("edit_file", {"path": "README.md"}))  # invalid arguments
    assert user.seen == []


# --- edit_file ------------------------------------------------------------------


def test_edit_file_replaces_the_text_and_shows_the_result(tools, user, project):
    result = ok(
        tools.run(
            "edit_file",
            {
                "path": "src/app/util.py",
                "old_string": "return 42",
                "new_string": "return 43",
            },
        )
    )
    assert (project / "src/app/util.py").read_text() == "def helper():\n    return 43\n"
    assert "replaced 1 occurrence" in result.content
    assert (
        "     2\t    return 43" in result.content
    )  # the model sees the new lines, numbered
    assert result.summary == "src/app/util.py +1 −1"


def test_the_user_is_shown_a_diff_of_the_edit(tools, user):
    ok(
        tools.run(
            "edit_file",
            {
                "path": "src/app/util.py",
                "old_string": "return 42",
                "new_string": "return 43",
            },
        )
    )
    request = user.seen[0]
    assert request.title == "Edit src/app/util.py"
    assert "-    return 42" in request.detail and "+    return 43" in request.detail


def test_edit_file_refuses_text_that_is_missing_or_ambiguous(tools, project):
    (project / "twice.txt").write_text("a = 1\nb = 2\na = 1\n")
    assert (
        "was not found"
        in err(
            tools.run(
                "edit_file", {"path": "twice.txt", "old_string": "c", "new_string": "d"}
            )
        ).content
    )
    ambiguous = err(
        tools.run(
            "edit_file",
            {"path": "twice.txt", "old_string": "a = 1", "new_string": "a = 9"},
        )
    )
    assert "occurs 2 times" in ambiguous.content and "lines 1, 3" in ambiguous.content
    assert (project / "twice.txt").read_text() == "a = 1\nb = 2\na = 1\n"  # untouched


def test_edit_file_can_replace_every_occurrence(tools, project):
    (project / "twice.txt").write_text("a = 1\nb = 2\na = 1\n")
    args = {
        "path": "twice.txt",
        "old_string": "a = 1",
        "new_string": "a = 9",
        "replace_all": True,
    }
    assert "all 2 occurrences" in ok(tools.run("edit_file", args)).content
    assert (project / "twice.txt").read_text() == "a = 9\nb = 2\na = 9\n"


def test_edit_file_can_delete_text(tools, project):
    ok(
        tools.run(
            "edit_file",
            {"path": "README.md", "old_string": "Hello world\n", "new_string": ""},
        )
    )
    assert (project / "README.md").read_text() == "# Demo\n"


def test_edit_file_keeps_windows_line_endings(tools, project):
    (project / "win.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    ok(
        tools.run(
            "edit_file",
            {"path": "win.txt", "old_string": "one\ntwo", "new_string": "one\n2"},
        )
    )
    assert (project / "win.txt").read_bytes() == b"one\r\n2\r\nthree\r\n"


@pytest.mark.parametrize(
    "args, message",
    [
        (
            {"path": "missing.py", "old_string": "a", "new_string": "b"},
            "use write_file",
        ),
        ({"path": "src", "old_string": "a", "new_string": "b"}, "is a folder"),
        ({"path": "logo.png", "old_string": "a", "new_string": "b"}, "binary"),
        (
            {"path": "README.md", "old_string": "", "new_string": "b"},
            "old_string is empty",
        ),
        (
            {"path": "README.md", "old_string": "Demo", "new_string": "Demo"},
            "nothing to change",
        ),
        (
            {"path": "../outside.txt", "old_string": "a", "new_string": "b"},
            "outside the working directory",
        ),
        (
            {"path": ".git/config", "old_string": "hello", "new_string": "bye"},
            "inside .git",
        ),
    ],
)
def test_edit_file_explains_what_went_wrong(tools, args, message):
    assert message in err(tools.run("edit_file", args)).content


# --- write_file -----------------------------------------------------------------


def test_write_file_creates_a_file_and_its_folders(tools, user, project):
    result = ok(
        tools.run(
            "write_file", {"path": "docs/guide/intro.md", "content": "# Intro\nHi\n"}
        )
    )
    assert (project / "docs/guide/intro.md").read_text() == "# Intro\nHi\n"
    assert result.summary == "created docs/guide/intro.md, 2 lines"
    assert (
        user.seen[0].title == "Create docs/guide/intro.md"
        and user.seen[0].detail == "# Intro\nHi\n"
    )


def test_write_file_overwrites_and_shows_the_user_what_would_be_lost(
    tools, user, project
):
    result = ok(
        tools.run("write_file", {"path": "README.md", "content": "# Demo\nGoodbye\n"})
    )
    assert (project / "README.md").read_text() == "# Demo\nGoodbye\n"
    assert result.summary == "overwrote README.md +1 −1"
    assert (
        user.seen[0].title == "Overwrite README.md"
        and "-Hello world" in user.seen[0].detail
    )


@pytest.mark.parametrize(
    "path, message",
    [
        ("src", "is a folder"),
        ("logo.png", "binary"),
        ("../x.txt", "outside"),
        (".git/hooks/pre-commit", "inside .git"),
    ],
)
def test_write_file_explains_what_went_wrong(tools, path, message):
    assert (
        message in err(tools.run("write_file", {"path": path, "content": "x"})).content
    )


def test_write_file_cannot_escape_through_a_symlinked_folder(
    tools, project, tmp_path_factory
):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    os.symlink(elsewhere, project / "link")
    assert (
        "outside"
        in err(tools.run("write_file", {"path": "link/x.txt", "content": "x"})).content
    )
    assert not (elsewhere / "x.txt").exists()


# --- shell ----------------------------------------------------------------------
# These run real commands, so they stick to ones every POSIX shell has.

WHY = "A sentence saying what the command is for."

posix_only = pytest.mark.skipif(os.name != "posix", reason="uses POSIX shell commands")


@posix_only
def test_shell_runs_in_the_working_directory_and_returns_output(tools):
    result = ok(tools.run("shell", {"description": WHY, "command": "ls src"}))
    assert result.content == "app\nnotes.txt"
    assert result.summary == "2 lines"


@posix_only
def test_shell_reports_a_failing_command_without_calling_it_a_tool_error(tools):
    result = ok(
        tools.run("shell", {"description": WHY, "command": "echo oops >&2; exit 3"})
    )
    assert result.content.startswith(
        "oops\n\n[Exit code 3]"
    )  # then, on macOS, a note about the sandbox
    assert result.summary == "exit 3 · 1 line"


@posix_only
def test_shell_says_when_there_is_no_output(tools):
    result = ok(tools.run("shell", {"description": WHY, "command": "true"}))
    assert (result.content, result.summary) == ("(no output)", "no output")


@posix_only
def test_shell_stops_a_command_that_runs_too_long_and_keeps_its_output(tools):
    # `sleep` runs as a child of the shell, so this also checks the whole
    # process group is killed: otherwise the test would hang for 30 seconds.
    result = err(
        tools.run(
            "shell",
            {"description": WHY, "command": "echo started; sleep 30", "timeout": 1},
        )
    )
    assert result.content.startswith("started")
    assert "still running after 1 seconds" in result.content


@posix_only
def test_shell_commands_get_no_keyboard_input(tools):
    assert (
        ok(tools.run("shell", {"description": WHY, "command": "cat"})).content
        == "(no output)"
    )  # returns at once


@posix_only
def test_shell_keeps_the_end_of_very_long_output(tools, monkeypatch):
    monkeypatch.setattr(shell_module, "MAX_OUTPUT_CHARS", 20)
    result = ok(tools.run("shell", {"description": WHY, "command": "seq 1 100"}))
    assert "showing the last 20 of" in result.content and result.content.endswith(
        "99\n100"
    )


@posix_only
def test_shell_gives_up_early_when_asked_to_stop(tools):
    # A front end that can cancel (the full-screen app's Escape) sets this hook.
    tools.context.should_stop = lambda: True
    result = err(
        tools.run("shell", {"description": WHY, "command": "echo started; sleep 30"})
    )
    assert (
        result.content.startswith("started") and "Stopped by the user" in result.content
    )


def test_shell_must_be_told_what_the_command_is_for(tools, user):
    missing = err(tools.run("shell", {"command": "true"}))
    assert "missing required argument 'description'" in missing.content
    assert (
        "'description'"
        in err(tools.run("shell", {"description": "x", "command": "true"})).content
    )  # too short to mean anything
    assert user.seen == []  # refused before the user is troubled

    ok(
        tools.run(
            "shell",
            {
                "description": "Do nothing, successfully.",
                "command": "true",
                "outside_sandbox": True,  # asked about, sandbox or not
            },
        )
    )
    assert (
        user.seen[0].explanation == "Do nothing, successfully."
        and user.seen[0].detail == "true"
    )


def test_only_commands_carry_an_explanation(tools, user):
    ok(tools.run("write_file", {"path": "n.txt", "content": "x"}))
    assert user.seen[0].explanation == ""  # a diff explains itself


def test_tool_calls_are_described_in_plain_english():
    def said(name, **args):
        return plain_call(ToolUseBlock("t", name, args))

    assert said("read_file", path="a.py") == ("Read a.py", "")
    assert said("read_file", path="a.py", offset=40) == ("Read a.py from line 40", "")
    assert said("list_files") == ("List *", "")
    assert said("list_files", pattern="**/*.py", path="src") == (
        "List **/*.py in src",
        "",
    )
    assert said("search", pattern="def main") == ("Search for 'def main'", "")
    assert said("edit_file", path="a.py", old_string="x", new_string="y") == (
        "Edit a.py",
        "",
    )
    assert said("write_file", path="a.py", content="x") == ("Write a.py", "")
    assert said(
        "shell",
        description="Count the Python files.",
        command="find . -name '*.py'\n | wc -l",
    ) == (
        "Count the Python files.",
        "$ find . -name '*.py' | wc -l",
    )
    assert said("some_new_tool", x=1) == (
        "some_new_tool(x=1)",
        "",
    )  # no words for it: the call itself


def test_shell_refuses_an_empty_command_and_a_silly_timeout(tools, user):
    assert (
        "command is empty"
        in err(tools.run("shell", {"description": WHY, "command": "  "})).content
    )
    assert (
        "less than or equal to 600"
        in err(
            tools.run("shell", {"description": WHY, "command": "true", "timeout": 9999})
        ).content
    )
    assert user.seen == []


# --- Auto mode and the sandbox -------------------------------------------------------
# The sandbox tests run real commands under macOS's sandbox-exec, and are
# skipped where it doesn't exist.

sandbox_only = pytest.mark.skipif(
    not sandbox.available(), reason="needs macOS's sandbox-exec"
)


@pytest.mark.parametrize(
    "path, contained",
    [
        ("src/new.py", True),
        ("envrc.txt", True),
        (".github/workflows/ci.yml", True),  # runs on GitHub's machines, not yours
        (".envrc", False),  # direnv runs it when you cd into the folder
        (".vscode/tasks.json", False),  # editors run tasks when the folder opens
        ("sub/.claude/settings.json", False),  # another agent's hooks, in any folder
    ],
)
def test_an_edit_is_contained_unless_it_could_run_by_itself(
    tools, user, path, contained
):
    ok(tools.run("write_file", {"path": path, "content": "x"}))
    assert user.seen[0].contained is contained


def test_auto_mode_allows_contained_actions_and_asks_about_the_rest(
    tools, user, project
):
    tools.permissions.auto = True
    user.decision = Decision.DENY
    ok(tools.run("write_file", {"path": "a.txt", "content": "1"}))
    ok(tools.run("edit_file", {"path": "a.txt", "old_string": "1", "new_string": "2"}))
    assert user.seen == [] and (project / "a.txt").read_text() == "2"

    assert (
        err(tools.run("write_file", {"path": ".envrc", "content": "x"})).content
        == DECLINED
    )
    outside = {"description": WHY, "command": "true", "outside_sandbox": True}
    assert err(tools.run("shell", outside)).content == DECLINED
    assert len(user.seen) == 2 and not (project / ".envrc").exists()


@posix_only
def test_every_command_is_asked_about_where_there_is_no_sandbox(
    tools, user, monkeypatch
):
    monkeypatch.setattr(sandbox, "available", lambda: False)
    for auto in (False, True):
        tools.permissions.auto = auto
        ok(tools.run("shell", {"description": WHY, "command": "true"}))
    assert [r.title for r in user.seen] == ["Run a shell command"] * 2
    assert not any(r.contained for r in user.seen)


def test_only_a_command_that_leaves_the_sandbox_is_asked_about(tools, monkeypatch):
    # The decision on its own, so it's checked on every system: nothing runs.
    monkeypatch.setattr(sandbox, "available", lambda: True)
    shell = tools.get("shell")
    inside = shell.parse({"description": WHY, "command": "uv sync"})
    outside = shell.parse(
        {"description": WHY, "command": "uv sync", "outside_sandbox": True}
    )
    assert shell.permission(inside, tools.context) is None
    request = shell.permission(outside, tools.context)
    assert request.title == "Run a shell command outside the sandbox"
    assert (request.scope, request.contained) == ("shell-outside:uv", False)


@pytest.mark.parametrize("has_sandbox", [True, False])
def test_the_auto_command_switches_the_mode_and_permissions_describes_it(
    tools, monkeypatch, has_sandbox
):
    monkeypatch.setattr("wren.commands.sandbox_available", lambda: has_sandbox)
    agent = Agent(FakeProvider(), "sys", 100, tools)
    shown = run_command(agent, "/permissions").text
    if has_sandbox:
        assert shown.startswith("Commands in the sandbox (no network")
        assert "asks before every edit, and every command outside the sandbox" in shown
    else:
        assert "Wren asks before every edit and command" in shown
    assert (
        run_command(agent, "/auto").text.startswith("Auto mode is on.")
        and tools.permissions.auto
    )
    shown = run_command(agent, "/permissions").text
    assert (
        shown.startswith("Auto mode is on.")
        and "Wren asks before everything else" in shown
    )
    assert (
        run_command(agent, "/auto").text.startswith("Auto mode is off")
        and not tools.permissions.auto
    )


@sandbox_only
def test_a_sandboxed_command_runs_without_asking_in_either_mode(tools, user):
    for auto in (False, True):
        tools.permissions.auto = auto
        result = ok(
            tools.run(
                "shell",
                {"description": WHY, "command": "echo hi > out.txt && cat out.txt"},
            )
        )
        assert result.content == "hi"
    assert user.seen == []


@sandbox_only
def test_a_sandboxed_command_writes_only_in_the_project(
    tools, project, tmp_path_factory, monkeypatch
):
    # The project is itself in a temp folder, and commands may write to
    # those, so leave them out: then "elsewhere" really is somewhere else.
    monkeypatch.setattr(sandbox, "temp_dirs", list)
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    ok(tools.run("shell", {"description": WHY, "command": "touch inside.txt"}))
    assert (project / "inside.txt").exists()

    for target in [
        elsewhere / "x.txt",
        project / ".git" / "sneaky",
        project / ".envrc",
    ]:
        result = ok(
            tools.run(
                "shell",
                {"description": WHY, "command": f"touch {shlex.quote(str(target))}"},
            )
        )
        assert "Operation not permitted" in result.content and not target.exists()
        assert shell_module.SANDBOX_NOTE in result.content  # so the model knows why


@sandbox_only
def test_a_sandboxed_command_cannot_write_protected_names_in_any_folder(tools, project):
    # A git worktree in the project is a checkout of its own: its `.git` file
    # says where its history is, and its `.claude` and `.envrc` run things.
    worktree = project / ".worktrees" / "fix"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /repo/.git/worktrees/fix\n")
    for command in [
        "echo 'gitdir: /tmp/elsewhere' > .worktrees/fix/.git",
        "mkdir .worktrees/fix/.claude",
        "touch .worktrees/fix/.envrc",
        "mkdir -p src/deep/.vscode",
    ]:
        result = ok(tools.run("shell", {"description": WHY, "command": command}))
        assert "Operation not permitted" in result.content, command
    assert (worktree / ".git").read_text() == "gitdir: /repo/.git/worktrees/fix\n"
    assert (
        not any((worktree / name).exists() for name in [".claude", ".envrc"])
        and not (project / "src/deep/.vscode").exists()
    )

    # Names that only start the same way are ordinary files.
    ok(
        tools.run(
            "shell",
            {
                "description": WHY,
                "command": "cd .worktrees/fix && touch .gitignore .github x.py",
            },
        )
    )
    assert all((worktree / name).exists() for name in [".gitignore", ".github", "x.py"])


@sandbox_only
def test_a_project_inside_a_protected_folder_is_still_writable(tmp_path_factory):
    # Say Wren is started in an old worktree under .claude/worktrees/: only
    # the names below the project count, not the folders above it.
    root = tmp_path_factory.mktemp("home") / ".claude" / "worktrees" / "ci"
    root.mkdir(parents=True)
    tools = ToolRegistry(default_tools(), root=root, permissions=Permissions(Answers()))
    ok(tools.run("shell", {"description": WHY, "command": "touch inside.txt"}))
    assert (root / "inside.txt").exists()
    result = ok(tools.run("shell", {"description": WHY, "command": "touch .envrc"}))
    assert (
        "Operation not permitted" in result.content and not (root / ".envrc").exists()
    )


@sandbox_only
def test_a_sandboxed_command_has_no_network(tools):
    # This connects to the computer itself, so nothing leaves it either way.
    # Outside the sandbox that's "connection refused"; inside, it isn't allowed at all.
    code = "import socket; socket.create_connection(('127.0.0.1', 9), 1)"
    result = ok(
        tools.run(
            "shell",
            {
                "description": WHY,
                "command": f'{shlex.quote(sys.executable)} -c "{code}"',
            },
        )
    )
    assert "Operation not permitted" in result.content


@sandbox_only
def test_a_sandboxed_command_cannot_read_credentials(
    tools, tmp_path_factory, monkeypatch
):
    home = tmp_path_factory.mktemp("home")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_ed25519").write_text("secret")
    monkeypatch.setenv("HOME", str(home))
    result = ok(
        tools.run("shell", {"description": WHY, "command": "cat ~/.ssh/id_ed25519"})
    )
    assert (
        "Operation not permitted" in result.content and "secret" not in result.content
    )


@sandbox_only
def test_outside_the_sandbox_is_asked_about_even_in_auto_mode(
    tools, user, tmp_path_factory
):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    tools.permissions.auto = True
    ok(
        tools.run(
            "shell",
            {
                "description": WHY,
                "command": f"touch {elsewhere}/x.txt",
                "outside_sandbox": True,
            },
        )
    )
    assert (elsewhere / "x.txt").exists()
    assert (
        user.seen[0].title == "Run a shell command outside the sandbox"
        and not user.seen[0].contained
    )


@sandbox_only
def test_a_command_that_ran_in_the_sandbox_is_still_asked_about_outside_it(tools, user):
    ok(tools.run("shell", {"description": WHY, "command": "true"}))  # no question
    user.decision = Decision.DENY
    err(
        tools.run(
            "shell", {"description": WHY, "command": "true", "outside_sandbox": True}
        )
    )
    assert [r.scope for r in user.seen] == ["shell-outside:true"]
