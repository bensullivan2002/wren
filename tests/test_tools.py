"""Tests for the tool framework (validation, registry, path safety) and the three file tools.

Each test builds a small throwaway project in pytest's `tmp_path`.
"""

import os

import pytest

from wren.tools import Tool, ToolError, ToolRegistry, ToolResult, default_tools
from wren.tools import list_files as list_files_module
from wren.tools import search as search_module
from wren.tools.files import glob_to_regex
from wren.tools.validation import validate_args


@pytest.fixture
def project(tmp_path):
    (tmp_path / "README.md").write_text("# Demo\nHello world\n")
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "app" / "main.py").write_text("def main():\n    print('Hello')\n")
    (tmp_path / "src" / "app" / "util.py").write_text("def helper():\n    return 42\n")
    (tmp_path / "src" / "notes.txt").write_text("TODO: hello again\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("hello from git\n")
    (tmp_path / "node_modules" / "lib").mkdir(parents=True)
    (tmp_path / "node_modules" / "lib" / "index.js").write_text("hello from node\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0hello")
    return tmp_path


@pytest.fixture
def tools(project):
    return ToolRegistry(default_tools(), root=project)


def ok(result):
    assert not result.is_error, result.content
    return result


def err(result):
    assert result.is_error, result.content
    return result


# --- validate_args ------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "default": 10},
        "loud": {"type": "boolean", "default": False},
    },
    "required": ["path"],
}


def test_valid_args_get_defaults_filled_in():
    assert validate_args({"path": "a"}, SCHEMA) == {"path": "a", "limit": 10, "loud": False}
    assert validate_args({"path": "a", "limit": 3}, SCHEMA)["limit"] == 3


def test_every_problem_is_reported_at_once():
    with pytest.raises(ToolError) as e:
        validate_args({"limit": "ten", "colour": "red"}, SCHEMA)
    message = str(e.value)
    assert "missing required argument 'path'" in message
    assert "'limit' must be an integer, not a string" in message
    assert "unknown argument 'colour'" in message
    assert "Accepted arguments: path, limit, loud" in message


def test_true_is_not_accepted_as_an_integer():
    with pytest.raises(ToolError, match="'limit' must be an integer, not a boolean"):
        validate_args({"path": "a", "limit": True}, SCHEMA)


def test_minimum_is_enforced():
    with pytest.raises(ToolError, match="at least 1"):
        validate_args({"path": "a", "limit": 0}, SCHEMA)


def test_arguments_must_be_an_object():
    with pytest.raises(ToolError, match="JSON object"):
        validate_args(["a"], SCHEMA)


# --- The registry -------------------------------------------------------------


class Boom(Tool):
    name = "boom"
    description = "Always crashes."
    input_schema = {"type": "object", "properties": {}}

    def run(self, args, ctx):
        raise ZeroDivisionError("division by zero")


class Big(Tool):
    name = "big"
    description = "Returns a lot of text."
    input_schema = {"type": "object", "properties": {}}

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
    result = ok(ToolRegistry([Big()], root=tmp_path, max_output_chars=100).run("big", {}))
    assert result.content.startswith("x" * 100)
    assert "showing the first 100 of 500 characters" in result.content


def test_duplicate_tool_names_are_refused(tmp_path):
    with pytest.raises(ValueError):
        ToolRegistry([Boom(), Boom()], root=tmp_path)


# --- Staying inside the working directory ---------------------------------------


@pytest.mark.parametrize("path", ["../outside.txt", "src/../../outside.txt", "/etc/hostname"])
def test_paths_outside_the_working_directory_are_refused(tools, path):
    assert "outside the working directory" in err(tools.run("read_file", {"path": path})).content


def test_a_symlink_pointing_outside_is_refused(tools, project, tmp_path_factory):
    secret = tmp_path_factory.mktemp("elsewhere") / "secret.txt"
    secret.write_text("password=hunter2\n")
    os.symlink(secret, project / "innocent.txt")

    assert "outside the working directory" in err(tools.run("read_file", {"path": "innocent.txt"})).content
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
    assert "offset" in err(tools.run("read_file", {"path": "long.txt", "offset": 11})).content


@pytest.mark.parametrize(
    "path, message",
    [("missing.py", "No such file"), ("src", "is a folder"), ("logo.png", "binary")],
)
def test_read_file_explains_what_went_wrong(tools, path, message):
    assert message in err(tools.run("read_file", {"path": path})).content


def test_read_file_handles_empty_files_and_long_lines(tools, project):
    (project / "empty.txt").write_text("")
    assert ok(tools.run("read_file", {"path": "empty.txt"})).content == "(this file is empty)"
    (project / "wide.txt").write_text("y" * 2000)
    assert "1,500 more characters" in ok(tools.run("read_file", {"path": "wide.txt"})).content


# --- list_files ---------------------------------------------------------------


def test_list_files_shows_one_folder_by_default(tools):
    listing = ok(tools.run("list_files", {})).content.splitlines()
    assert listing == ["src/", "README.md", "logo.png"]  # .git and node_modules are skipped


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
    assert result.content.splitlines() == ["README.md:2: Hello world", "src/app/main.py:2:     print('Hello')"]
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
    assert "Invalid regular expression" in err(tools.run("search", {"pattern": "("})).content
    assert ok(tools.run("search", {"pattern": "zebra"})).summary == "no matches"


def test_search_caps_matches(tools, monkeypatch):
    monkeypatch.setattr(search_module, "MAX_MATCHES", 1)
    result = ok(tools.run("search", {"pattern": "e"}))
    assert len(result.content.split("\n\n")[0].splitlines()) == 1
    assert "Showing the first 1 of" in result.content
