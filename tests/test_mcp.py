"""MCP: reading the config, wrapping server tools, and talking to a real server.

Most tests use a fake `Caller`, so they need no server process. The ones at
the end start tests/mcp_demo_server.py over stdio, through the real SDK.
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest
from mcp.types import (
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
    TextResourceContents,
)
from mcp.types import Tool as McpToolInfo

from wren import mcp_config
from wren.agent import Agent
from wren.commands import plain_call, run_command
from wren.mcp_config import ServerConfig
from wren.mcp_servers import McpCallError, McpServers, ServerStatus
from wren.messages import Message, TextBlock, ToolUseBlock, history_problems
from wren.providers.fake import FakeProvider
from wren.tools import Decision, Permissions, ToolRegistry, default_tools
from wren.tools.mcp_tool import REASON, McpTool, render, tools_for

DEMO = Path(__file__).with_name("mcp_demo_server.py")


# --- Test doubles -------------------------------------------------------------


class FakeCaller:
    """Stands in for McpServers: records calls, returns a canned result."""

    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result or CallToolResult(
            content=[TextContent(type="text", text="ok")]
        )
        self.error = error

    def call(self, server, tool, arguments, should_stop=None, timeout=None):
        self.calls.append((server, tool, arguments))
        if self.error:
            raise self.error
        return self.result


def info(name="create_issue", schema=None, description="Create an issue."):
    schema = schema or {
        "type": "object",
        "properties": {"title": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["title"],
        "additionalProperties": False,
    }
    return McpToolInfo(name=name, description=description, input_schema=schema)


class Recorder:
    """A permission asker that remembers every question and gives a fixed answer."""

    def __init__(self, decision=Decision.ALLOW):
        self.decision = decision
        self.asked = []

    def __call__(self, request):
        self.asked.append(request)
        return self.decision


def registry_with(tool, decision=Decision.ALLOW, auto=False):
    asker = Recorder(decision)
    permissions = Permissions(asker)
    permissions.auto = auto
    return ToolRegistry([tool], permissions=permissions), asker


# --- The config file ----------------------------------------------------------


def write(tmp_path, data):
    path = tmp_path / "mcp.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_no_config_file_means_no_servers_and_no_problems(tmp_path):
    config = mcp_config.load(tmp_path / "missing.json", environ={})
    assert config.servers == [] and config.problems == []


def test_a_server_entry_is_read_with_variables_expanded(tmp_path):
    path = write(
        tmp_path,
        {
            "mcpServers": {
                "github": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["-y", "server-${FLAVOUR:-github}"],
                    "env": {"TOKEN": "${GH_TOKEN}"},
                }
            }
        },
    )
    config = mcp_config.load(path, environ={"GH_TOKEN": "secret"})
    assert config.problems == []
    assert config.servers == [
        ServerConfig("github", "npx", ["-y", "server-github"], {"TOKEN": "secret"})
    ]


def test_an_unset_variable_skips_that_server_only(tmp_path):
    path = write(
        tmp_path,
        {
            "mcpServers": {
                "a": {"command": "x", "env": {"T": "${NOT_SET}"}},
                "b": {"command": "y"},
            }
        },
    )
    config = mcp_config.load(path, environ={})
    assert [s.name for s in config.servers] == ["b"]
    assert "NOT_SET isn't set" in config.problems[0]


@pytest.mark.parametrize(
    "entry, words",
    [
        ({"command": "x", "arg": ["typo"]}, "unknown key 'arg'"),
        ({"url": "https://example.com/mcp"}, "remote (HTTP) servers"),
        ({"type": "http", "command": "x"}, "remote (HTTP) servers"),
        ({"args": []}, '"command"'),
        ({"command": "x", "args": "not a list"}, '"args"'),
        ({"command": "x", "env": {"N": 1}}, '"env"'),
    ],
)
def test_a_bad_entry_is_skipped_with_a_reason(tmp_path, entry, words):
    config = mcp_config.load(write(tmp_path, {"mcpServers": {"s": entry}}), environ={})
    assert config.servers == []
    assert words in config.problems[0]


def test_a_server_name_that_cant_be_part_of_a_tool_name_is_refused(tmp_path):
    config = mcp_config.load(
        write(tmp_path, {"mcpServers": {"my server": {"command": "x"}}}), environ={}
    )
    assert config.servers == [] and "names may use only" in config.problems[0]


def test_broken_json_is_a_problem_not_a_crash(tmp_path):
    config = mcp_config.load(write(tmp_path, "{nope"), environ={})
    assert config.servers == [] and "isn't valid JSON" in config.problems[0]


# --- Wrapping a server's tool -------------------------------------------------


def test_the_model_sees_the_servers_schema_plus_a_reason():
    spec = McpTool("github", info(), FakeCaller()).spec()

    assert spec.name == "mcp__github__create_issue"
    assert "Create an issue." in spec.description and "'github'" in spec.description
    assert set(spec.input_schema["properties"]) == {"title", "count", REASON}
    assert spec.input_schema["required"] == ["title", REASON]
    assert spec.input_schema["additionalProperties"] is False


def test_the_servers_schema_itself_is_not_changed():
    tool_info = info()
    McpTool("github", tool_info, FakeCaller()).spec()
    assert REASON not in tool_info.input_schema["properties"]


def test_a_call_asks_with_the_reason_first_and_the_arguments_underneath():
    caller = FakeCaller()
    registry, asker = registry_with(McpTool("github", info(), caller))

    result = registry.run(
        "mcp__github__create_issue", {"title": "Bug", REASON: "File the bug we found."}
    )

    [request] = asker.asked
    assert request.explanation == "File the bug we found."
    assert json.loads(request.detail) == {"title": "Bug"}
    assert request.contained is False
    assert "create_issue" in request.title and "github" in request.title
    # The server never sees wren_reason.
    assert caller.calls == [("github", "create_issue", {"title": "Bug"})]
    assert result.content == "ok" and not result.is_error


def test_declining_means_the_server_is_never_called():
    caller = FakeCaller()
    registry, _ = registry_with(McpTool("github", info(), caller), Decision.DENY)

    result = registry.run("mcp__github__create_issue", {"title": "Bug", REASON: "x"})

    assert result.is_error and caller.calls == []


def test_always_covers_that_one_tool_for_the_session():
    caller = FakeCaller()
    other = McpTool("github", info("close_issue"), caller)
    asker = Recorder(Decision.ALWAYS)
    registry = ToolRegistry(
        [McpTool("github", info(), caller), other], permissions=Permissions(asker)
    )

    registry.run("mcp__github__create_issue", {"title": "a", REASON: "x"})
    registry.run("mcp__github__create_issue", {"title": "b", REASON: "x"})
    registry.run("mcp__github__close_issue", {"title": "c", REASON: "x"})

    assert len(asker.asked) == 2  # once for create_issue, once for close_issue
    assert len(caller.calls) == 3


def test_auto_mode_still_asks():
    registry, asker = registry_with(McpTool("github", info(), FakeCaller()), auto=True)
    registry.run("mcp__github__create_issue", {"title": "a", REASON: "x"})
    assert len(asker.asked) == 1


def test_a_call_without_a_reason_is_refused_before_asking():
    caller = FakeCaller()
    registry, asker = registry_with(McpTool("github", info(), caller))

    result = registry.run("mcp__github__create_issue", {"title": "Bug"})

    assert result.is_error and REASON in result.content
    assert asker.asked == [] and caller.calls == []


def test_arguments_are_checked_against_the_servers_schema_before_asking():
    caller = FakeCaller()
    registry, asker = registry_with(McpTool("github", info(), caller))

    result = registry.run(
        "mcp__github__create_issue", {"count": "three", "extra": 1, REASON: "x"}
    )

    assert result.is_error
    assert "'title' is a required property" in result.content
    assert "'count'" in result.content
    assert asker.asked == [] and caller.calls == []


def test_a_broken_server_schema_leaves_checking_to_the_server():
    tool = McpTool(
        "s",
        info(schema={"type": "object", "properties": {"x": {"type": "nonsense"}}}),
        FakeCaller(),
    )
    assert tool.parse({"x": 1, REASON: "r"}).arguments == {"x": 1}


def test_an_error_result_reaches_the_model_as_an_error():
    caller = FakeCaller(
        CallToolResult(
            content=[TextContent(type="text", text="Not found")], is_error=True
        )
    )
    registry, _ = registry_with(McpTool("github", info(), caller))

    result = registry.run("mcp__github__create_issue", {"title": "a", REASON: "x"})

    assert result.is_error and result.content == "Not found"


def test_a_call_that_gets_no_result_says_why():
    caller = FakeCaller(
        error=McpCallError("The MCP server 'github' didn't answer within 300 seconds.")
    )
    registry, _ = registry_with(McpTool("github", info(), caller))

    result = registry.run("mcp__github__create_issue", {"title": "a", REASON: "x"})

    assert result.is_error and "didn't answer" in result.content


def test_names_are_made_safe_and_clashes_and_long_names_are_skipped():
    taken = {"read_file", "mcp__s__taken"}
    infos = [info("with.dot"), info("taken"), info("x" * 80)]

    tools, skipped = tools_for("s", infos, FakeCaller(), taken)

    assert [t.name for t in tools] == ["mcp__s__with_dot"]
    assert tools[0].tool_name == "with.dot"  # what the server is asked for
    assert len(skipped) == 2


# --- Results as text ----------------------------------------------------------


def test_text_blocks_are_joined():
    result = CallToolResult(
        content=[TextContent(type="text", text="a"), TextContent(type="text", text="b")]
    )
    assert render(result) == "a\nb"


def test_things_wren_cant_pass_on_are_described_not_dropped():
    result = CallToolResult(
        content=[
            ImageContent(type="image", data="aGVsbG8=" * 400, mime_type="image/png"),
            ResourceLink(type="resource_link", name="notes", uri="file:///notes.md"),
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(uri="file:///a.txt", text="inside"),
            ),
        ]
    )
    text = render(result)
    assert "[image, image/png" in text and "not shown" in text
    assert "[resource link: notes file:///notes.md]" in text
    assert "inside" in text


def test_structured_content_is_used_when_there_is_no_text():
    assert json.loads(
        render(CallToolResult(content=[], structured_content={"n": 5}))
    ) == {"n": 5}


# --- How a call looks in the transcript, and /mcp -----------------------------


def test_a_call_reads_as_the_models_reason():
    call = ToolUseBlock(
        "t1", "mcp__github__create_issue", {"title": "Bug", REASON: "File the bug."}
    )
    assert plain_call(call) == ("File the bug.", "MCP: create_issue on github")


def test_mcp_command_explains_how_to_add_a_server_when_there_are_none():
    agent = Agent(FakeProvider([]), "sys", 100, ToolRegistry())
    result = run_command(agent, "/mcp")
    assert (
        "No MCP servers are configured" in result.text and "mcpServers" in result.text
    )


def test_mcp_command_shows_each_server_and_where_its_facts_come_from(tmp_path):
    servers = McpServers(mcp_config.McpConfig(problems=["MCP server 'x' skipped: bad"]))
    servers.statuses = [
        ServerStatus(
            "github", "npx server", tmp_path / "g.log", protocol_version="2026-07-28"
        ),
        ServerStatus(
            "broken",
            "nope",
            tmp_path / "b.log",
            error="couldn't find the program 'nope'",
        ),
    ]
    registry = ToolRegistry([McpTool("github", info(), FakeCaller())])
    registry.mcp = servers
    text = run_command(Agent(FakeProvider([]), "sys", 100, registry), "/mcp").text

    assert "running, 1 tool" in text and "mcp__github__create_issue" in text
    assert "2026-07-28 (agreed with the server" in text
    assert "not running: couldn't find the program 'nope'" in text
    assert "MCP server 'x' skipped: bad" in text


# --- In the agent loop --------------------------------------------------------


def test_an_mcp_tool_in_a_turn_leaves_a_valid_history():
    caller = FakeCaller()
    registry = ToolRegistry(
        [*default_tools(), McpTool("github", info(), caller)],
        permissions=Permissions(Recorder()),
    )
    call = ToolUseBlock(
        "t1", "mcp__github__create_issue", {"title": "Bug", REASON: "File it."}
    )
    provider = FakeProvider(
        [Message("assistant", [TextBlock("Filing."), call]), "Done."]
    )
    agent = Agent(provider, "sys", 100, registry)

    list(agent.run_turn("file the bug"))

    assert caller.calls == [("github", "create_issue", {"title": "Bug"})]
    assert history_problems(agent.messages) == []


# --- A real server, over stdio ------------------------------------------------


@pytest.fixture
def demo(tmp_path):
    servers = McpServers(log_dir=tmp_path)
    servers.start([ServerConfig("demo", sys.executable, [str(DEMO)])])
    yield servers
    servers.close()


def test_a_real_server_starts_and_lists_its_tools(demo):
    [status] = demo.statuses
    assert status.running, status.error
    assert {tool.name for tool in status.tools} == {"add", "fail", "slow", "pid"}
    assert status.protocol_version  # whichever version the two sides agreed


def test_a_real_server_runs_a_tool(demo):
    result = demo.call("demo", "add", {"a": 2, "b": 3})
    assert render(result) == "5" and not result.is_error


def test_a_real_servers_failing_tool_is_an_error_result(demo):
    result = demo.call("demo", "fail", {})
    assert result.is_error


def test_a_slow_call_stops_when_asked(demo):
    started = time.monotonic()
    with pytest.raises(McpCallError, match="Stopped"):
        demo.call(
            "demo",
            "slow",
            {"seconds": 30},
            should_stop=lambda: time.monotonic() - started > 0.5,
        )
    assert time.monotonic() - started < 5
    # The server is still usable afterwards.
    assert render(demo.call("demo", "add", {"a": 1, "b": 1})) == "2"


def test_a_slow_call_times_out(demo):
    with pytest.raises(McpCallError, match="didn't answer within"):
        demo.call("demo", "slow", {"seconds": 30}, timeout=0.5)


def test_a_server_that_cant_start_is_reported_and_others_still_run(tmp_path):
    with McpServers(log_dir=tmp_path) as servers:
        servers.start(
            [
                ServerConfig("missing", "no-such-program-for-wren-tests"),
                ServerConfig("demo", sys.executable, [str(DEMO)]),
            ]
        )
        missing, demo = servers.statuses
        assert not missing.running and "couldn't find the program" in missing.error
        assert demo.running
        with pytest.raises(McpCallError, match="isn't running"):
            servers.call("missing", "anything", {})


def test_closing_stops_the_server_process(tmp_path):
    servers = McpServers(log_dir=tmp_path)
    servers.start([ServerConfig("demo", sys.executable, [str(DEMO)])])
    pid = int(render(servers.call("demo", "pid", {})))
    servers.close()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # signal 0 checks a process exists, without touching it
    with pytest.raises(McpCallError, match="isn't running"):
        servers.call("demo", "add", {"a": 1, "b": 1})


def test_the_fake_model_can_call_an_mcp_tool_for_the_offline_demo():
    provider = FakeProvider()
    reply = provider._demo_reply([Message.user('mcp mcp__demo__add {"a": 2, "b": 3}')])
    [call] = reply.tool_uses
    assert call.name == "mcp__demo__add"
    assert call.input == {
        "a": 2,
        "b": 3,
        REASON: "Use the MCP tool you named after 'mcp'.",
    }
