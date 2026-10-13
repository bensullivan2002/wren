"""GitHub reads: fake CLI results, no network, credentials or installed gh needed."""

import os
import sys
from pathlib import Path

import pytest

from wren.agent import Agent
from wren.commands import plain_call, run_command
from wren.messages import Message, ToolUseBlock, history_problems
from wren.providers.fake import FakeProvider
from wren.tools import (
    Decision,
    Permissions,
    ToolRegistry,
    default_tools,
    sandbox,
    shell,
)
from wren.tools import github_read as github
from wren.tools.registry import DECLINED

REPO = "bensullivan2002/wren"
SHA = "a" * 40


@pytest.fixture
def harness(tmp_path, monkeypatch):
    calls = []
    answers = []
    prompts = []
    # A trusted executable outside the throwaway project. No real CLI is run.
    executable = str(Path(sys.executable).resolve())
    monkeypatch.setattr(github.shutil, "which", lambda name: executable)
    monkeypatch.setattr(github, "temp_dirs", list)

    def run(command, cwd, timeout, should_stop, *, env):
        calls.append((command, cwd, timeout, env))
        return answers.pop(0) if answers else ('{"ok":true}\n', 0, None)

    def ask(request):
        prompts.append(request)
        return Decision.DENY

    monkeypatch.setattr(github, "_run", run)
    tools = ToolRegistry(default_tools(), root=tmp_path, permissions=Permissions(ask))
    return tools, calls, answers, prompts, executable


def read(harness, operation="repo", **kwargs):
    return harness[0].run(
        "github_read", {"repo": REPO, "operation": operation, **kwargs}
    )


@pytest.mark.parametrize("auto", [False, True])
@pytest.mark.parametrize("has_sandbox", [False, True])
def test_reads_do_not_ask_on_any_platform_or_mode(
    harness, monkeypatch, auto, has_sandbox
):
    monkeypatch.setattr(sandbox, "available", lambda: has_sandbox)
    harness[0].permissions.auto = auto
    result = read(harness, "issue_view", number=12)
    assert not result.is_error
    assert result.content == '{"ok":true}'
    assert harness[3] == []
    command, cwd, timeout, env = harness[1][0]
    assert command == [
        harness[4],
        "api",
        "--hostname",
        "github.com",
        "--method",
        "GET",
        f"repos/{REPO}/issues/12",
    ]
    assert cwd == harness[0].context.root
    assert 0 < timeout <= 120
    assert env["GH_PROMPT_DISABLED"] == "1"


def test_reads_work_without_any_approval_callback(harness):
    harness[0].permissions = Permissions()
    assert not read(harness).is_error


@pytest.mark.parametrize(
    "operation,kwargs,endpoints",
    [
        ("repo", {}, [f"repos/{REPO}"]),
        (
            "issue_list",
            {"state": "all", "page": 2, "per_page": 5},
            [f"repos/{REPO}/issues?per_page=5&page=2&state=all"],
        ),
        (
            "pr_list",
            {"state": "closed"},
            [f"repos/{REPO}/pulls?per_page=30&page=1&state=closed"],
        ),
        ("issue_view", {"number": 12}, [f"repos/{REPO}/issues/12"]),
        ("pr_view", {"number": 12}, [f"repos/{REPO}/pulls/12"]),
        ("pr_diff", {"number": 12}, [f"repos/{REPO}/pulls/12"]),
        (
            "issue_comments",
            {"number": 12},
            [f"repos/{REPO}/issues/12/comments?per_page=30&page=1"],
        ),
        (
            "pr_comments",
            {"number": 12},
            [
                f"repos/{REPO}/issues/12/comments?per_page=30&page=1",
                f"repos/{REPO}/pulls/12/comments?per_page=30&page=1",
            ],
        ),
        (
            "pr_checks",
            {"number": 12, "page": 2},
            [
                f"repos/{REPO}/pulls/12",
                f"repos/{REPO}/commits/{SHA}/check-runs?per_page=30&page=2",
                f"repos/{REPO}/commits/{SHA}/status?per_page=30&page=2",
            ],
        ),
    ],
)
def test_every_operation_builds_only_fixed_get_requests(
    harness, operation, kwargs, endpoints
):
    if operation == "pr_checks":
        harness[2].append((SHA + "\n", 0, None))
    result = read(harness, operation, **kwargs)
    assert not result.is_error, result.content
    assert len(harness[1]) == len(endpoints)  # no automatic/unbounded pagination
    for (command, *_), endpoint in zip(harness[1], endpoints, strict=True):
        expected = [
            harness[4],
            "api",
            "--hostname",
            "github.com",
            "--method",
            "GET",
            endpoint,
        ]
        if operation == "pr_diff":
            expected += ["--header", "Accept: application/vnd.github.diff"]
        if operation == "pr_checks" and endpoint.endswith("/pulls/12"):
            expected += ["--jq", ".head.sha"]
        assert command == expected
    if operation == "pr_comments":
        assert (
            "Conversation comments:" in result.content
            and "Inline review comments:" in result.content
        )
    if operation == "pr_checks":
        assert "Check runs:" in result.content and "Commit statuses:" in result.content
    assert harness[3] == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"operation": "delete"},
        {"operation": "issue_close"},
        {"operation": "pr_merge"},
        {"operation": "issue_comment"},
        {"operation": "POST"},
        {"repo": "https://github.com/owner/repo"},
        {"repo": "owner/repo/../other"},
        {"repo": "owner/.."},
        {"repo": "../repo"},
        {"repo": "owner/repo?x=1"},
        {"repo": "owner/repo#fragment"},
        {"repo": "owner/{repo}"},
        {"repo": "owner/repo; rm -rf ~"},
        {"repo": "owner/repo\n"},
        {"repo": "owner/repo%2fother"},
        {"repo": "--hostname/evil.com"},
        {"repo": "owner/repo\\other"},
        {"repo": "owner/repo\x00"},
        {"endpoint": "graphql"},
        {"method": "DELETE"},
        {"hostname": "evil.com"},
        {"flags": ["--method", "POST"]},
        {"jq": "malicious"},
        {"command": "gh repo delete"},
        {"operation": "issue_view"},
        {"operation": "issue_comments"},
        {"operation": "pr_checks"},
        {"number": 12},
        {"operation": "issue_list", "number": 1},
        {"state": "closed"},
        {"page": 2},
        {"per_page": 100},
        {"operation": "pr_view", "number": 0},
        {"operation": "pr_view", "number": True},
        {"operation": "pr_view", "number": "12"},
        {"operation": "pr_list", "page": 0},
        {"operation": "pr_list", "page": 10001},
        {"operation": "pr_list", "per_page": 101},
        {"operation": "pr_list", "per_page": 0},
        {"timeout": 0},
        {"timeout": 601},
    ],
)
def test_invalid_requests_fail_before_any_execution_or_prompt(harness, kwargs):
    result = read(harness, **kwargs)
    assert result.is_error
    assert harness[1] == [] and harness[3] == []


@pytest.mark.parametrize("repo", ["Owner/Repo", "owner/a.b-c_d", "owner/.github"])
def test_valid_repository_names(harness, repo):
    # A leading dot is a valid GitHub repository name (e.g. an org's .github).
    assert not read(harness, repo=repo).is_error


@pytest.mark.parametrize(
    "sha", ["", "../../other", "a" * 41, "b" * 65, "x" * 40, "a" * 40 + "?x=1"]
)
def test_remote_sha_cannot_select_arbitrary_endpoints(harness, sha):
    harness[2].append((sha, 0, None))
    result = read(harness, "pr_checks", number=1)
    assert result.is_error and "invalid PR head" in result.content
    assert len(harness[1]) == 1


def test_debug_logging_is_disabled_without_changing_parent_environment(
    harness, monkeypatch
):
    monkeypatch.setenv("GH_DEBUG", "api")
    monkeypatch.setenv("GH_FORCE_TTY", "100%")
    monkeypatch.setenv("GH_TOKEN", "test-token")
    assert not read(harness).is_error
    env = harness[1][0][3]
    assert "GH_DEBUG" not in env and "GH_FORCE_TTY" not in env
    assert env["GH_TOKEN"] == "test-token"  # gh still authenticates, never print this
    assert "test-token" not in read(harness).content
    assert os.environ["GH_DEBUG"] == "api"


@pytest.mark.parametrize(
    "output,code,stopped,expected",
    [
        ("gh: HTTP 404 Not Found", 1, None, "repository access"),
        ("To get started, run gh auth login", 4, None, "gh auth"),
        ("could not resolve host", 1, None, "network connectivity"),
        ("partial", -9, "timeout", "timed out"),
        ("partial", -9, "cancelled", "stopped by the user"),
    ],
)
def test_cli_failures_are_readable_errors(harness, output, code, stopped, expected):
    harness[2].append((output, code, stopped))
    result = read(harness)
    assert result.is_error and expected in result.content


def test_failure_stops_a_multi_request_operation(harness):
    harness[2].append(("Forbidden", 1, None))
    assert read(harness, "pr_comments", number=1).is_error
    assert len(harness[1]) == 1


@pytest.mark.parametrize("code", [0, 1])
def test_success_and_failure_output_are_capped(harness, code):
    harness[2].append(("x" * 50_000, code, None))
    result = read(harness)
    assert result.is_error == bool(code)
    assert "Output truncated" in result.content
    assert len(result.content) <= github.MAX_OUTPUT_CHARS


def test_pagination_is_explicit_and_reports_page(harness):
    harness[2].append(("[]", 0, None))
    result = read(harness, "pr_list", page=3, per_page=10)
    assert not result.is_error
    assert "Page 3, up to 10 items" in result.content
    assert len(harness[1]) == 1


def test_timeout_is_shared_across_requests(harness, monkeypatch):
    ticks = iter([0, 1, 4, 6])
    monkeypatch.setattr(github.time, "monotonic", lambda: next(ticks))
    harness[2].append((SHA, 0, None))
    result = read(harness, "pr_checks", number=1, timeout=5)
    assert result.is_error and "timed out after 5s" in result.content
    assert [call[2] for call in harness[1]] == [4, 1]


def test_cancellation_between_requests_does_not_start_next_process(harness):
    stops = iter([False, True])
    harness[0].context.should_stop = lambda: next(stops)
    result = read(harness, "pr_comments", number=1)
    assert result.is_error and "stopped by the user" in result.content
    assert len(harness[1]) == 1


def test_missing_cli_does_not_ask_or_run(harness, monkeypatch):
    monkeypatch.setattr(github.shutil, "which", lambda _: None)
    result = read(harness)
    assert result.is_error and "not installed or not on PATH" in result.content
    assert not harness[1] and not harness[3]


@pytest.mark.parametrize("location", ["project", "temp", "symlink"])
def test_untrusted_executable_is_refused(harness, tmp_path, monkeypatch, location):
    fake = tmp_path / "gh"
    fake.write_text("not a trusted CLI")
    if location == "symlink":
        link = tmp_path / "link"
        link.symlink_to(fake)
        found = link
    else:
        found = fake
    monkeypatch.setattr(github.shutil, "which", lambda _: str(found))
    if location == "temp":
        harness[0].context.root = tmp_path / "separate-project"
        monkeypatch.setattr(github, "temp_dirs", lambda: [str(tmp_path)])
    result = read(harness)
    assert result.is_error and "Refusing a gh executable" in result.content
    assert not harness[1]


@pytest.mark.parametrize("auto", [False, True])
@pytest.mark.parametrize("has_sandbox", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "gh issue comment 12 --body hello",
        "gh issue close 12",
        "gh repo delete owner/repo --yes",
        "gh pr merge 12",
    ],
)
def test_github_writes_keep_existing_shell_approval(
    harness, monkeypatch, auto, has_sandbox, command
):
    monkeypatch.setattr(sandbox, "available", lambda: has_sandbox)
    harness[0].permissions.auto = auto
    # A declined write must never reach the shell runner either.
    monkeypatch.setattr(
        shell, "_run", lambda *a, **kw: pytest.fail("declined shell ran")
    )
    result = harness[0].run(
        "shell",
        {
            "command": command,
            "description": "Change GitHub state",
            "outside_sandbox": True,
        },
    )
    assert result.is_error and result.content == DECLINED
    assert len(harness[3]) == 1 and not harness[3][0].contained
    assert harness[0].permissions.always == {}


@pytest.mark.parametrize("has_sandbox", [False, True])
def test_permission_explanations_and_plain_call_work_in_both_frontends(
    harness, monkeypatch, has_sandbox
):
    monkeypatch.setattr("wren.commands.sandbox_available", lambda: has_sandbox)
    agent = Agent(FakeProvider(), "sys", 100, harness[0])
    for command in ["/permissions", "/auto", "/permissions", "/auto"]:
        text = run_command(agent, command).text
        assert "GitHub reads via github_read run without asking" in text
        assert "GitHub writes keep the shell's approval rules" in text
    call = ToolUseBlock(
        "g1", "github_read", {"repo": REPO, "operation": "issue_view", "number": 12}
    )
    assert plain_call(call) == (f"Read GitHub {REPO} #12", "issue_view")
    assert "github_read" in run_command(agent, "/tools").text


def test_agent_round_trip_keeps_history_valid(harness):
    call = ToolUseBlock(
        "g1", "github_read", {"repo": REPO, "operation": "issue_view", "number": 12}
    )
    provider = FakeProvider([Message("assistant", [call]), "Here is the issue."])
    agent = Agent(provider, "sys", 100, harness[0])
    list(agent.run_turn("Read issue 12"))
    result = agent.messages[2].tool_results[0]
    assert not result.is_error and result.tool_use_id == "g1"
    assert result.content == '{"ok":true}'
    assert history_problems(agent.messages) == []
    assert harness[3] == []


def test_argument_runner_does_not_use_shell_and_passes_environment(tmp_path):
    # Real subprocess, but no GitHub CLI, network or authentication.
    output, code, stopped = shell._run(
        [
            sys.executable,
            "-c",
            "import os, sys; print(os.environ['WREN_TEST']); print(sys.argv[1])",
            "literal;not-a-shell-command",
        ],
        tmp_path,
        5,
        lambda: False,
        env={"WREN_TEST": "present"},
    )
    assert code == 0 and stopped is None
    assert output.splitlines() == ["present", "literal;not-a-shell-command"]
