"""Tests for ACP mode: Wren driven over JSON-RPC, as Toad or an editor would.

Each test plays the client. `Client` below starts a real AcpServer on a pair
of pipes, sends it messages, and reads what comes back, so these exercise
the same bytes a real client would see.
"""

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from wren.acp import AcpServer, Connection
from wren.agent import INTERRUPTED_NOTE, Agent
from wren.messages import Message, TextBlock, ToolUseBlock, history_problems
from wren.providers.fake import FakeProvider
from wren.tools import ToolRegistry, default_tools

SRC = Path(__file__).resolve().parents[1] / "src"


class Client:
    """A minimal ACP client, for tests."""

    def __init__(self, replies, root):
        to_server_r, to_server_w = os.pipe()
        from_server_r, from_server_w = os.pipe()
        self._out = os.fdopen(to_server_w, "w", encoding="utf-8")
        self.provider = FakeProvider(replies)

        def make_agent(cwd, permissions):
            tools = ToolRegistry(default_tools(), root=cwd, permissions=permissions)
            return Agent(self.provider, "sys", 100, tools)

        connection = Connection(
            os.fdopen(to_server_r, encoding="utf-8"), os.fdopen(from_server_w, "w", encoding="utf-8")
        )
        self.server = AcpServer(connection, make_agent)
        threading.Thread(target=self.server.serve, daemon=True).start()

        # Read the server's output on a thread, so a test that expects a
        # message that never comes fails with a timeout and doesn't hang.
        self._inbox = queue.Queue()
        reader = os.fdopen(from_server_r, encoding="utf-8")
        threading.Thread(target=lambda: [self._inbox.put(json.loads(line)) for line in reader], daemon=True).start()

        self._next_id = 0
        self.root = root

    def send(self, message):
        self._out.write(json.dumps(message) + "\n")
        self._out.flush()

    def request(self, method, params):
        """Send a request and return its id (the response is read separately)."""
        self._next_id += 1
        self.send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        return self._next_id

    def read(self):
        return self._inbox.get(timeout=5)

    def read_until_response(self, id):
        """Return (everything that arrived before the response, the response)."""
        before = []
        while True:
            message = self.read()
            if message.get("id") == id and "method" not in message:
                return before, message
            before.append(message)

    def call(self, method, params):
        return self.read_until_response(self.request(method, params))[1]

    def new_session(self):
        self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        self.session_id = self.call("session/new", {"cwd": str(self.root), "mcpServers": []})["result"]["sessionId"]
        return self.session_id

    def prompt(self, text):
        return self.request(
            "session/prompt", {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]}
        )

    def wait_for_permission(self):
        """Wait for Wren to ask permission; return the request message."""
        while True:
            message = self.read()
            if message.get("method") == "session/request_permission":
                return message

    def answer_permission(self, option_id, asked=None):
        """Answer a permission request (waiting for it first unless given),
        and return what was asked. `None` answers "cancelled"."""
        asked = asked or self.wait_for_permission()
        outcome = {"outcome": "selected", "optionId": option_id} if option_id else {"outcome": "cancelled"}
        self.send({"jsonrpc": "2.0", "id": asked["id"], "result": {"outcome": outcome}})
        return asked["params"]

    def cancel_at_permission_prompt(self):
        """What a real client does when its user presses Escape at the
        prompt: send session/cancel, and answer the open question "cancelled"."""
        asked = self.wait_for_permission()
        self.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": self.session_id}})
        self.answer_permission(None, asked)

    @property
    def history(self):
        return self.server.sessions[self.session_id].agent.messages


def asks(*calls):
    return Message("assistant", [TextBlock("On it."), *(ToolUseBlock(i, n, a) for i, n, a in calls)])


def updates(messages):
    return [m["params"]["update"] for m in messages if m.get("method") == "session/update"]


# --- The handshake ----------------------------------------------------------------


def test_initialize_agrees_a_version_and_says_what_wren_can_do(tmp_path):
    result = Client([], tmp_path).call("initialize", {"protocolVersion": 1})["result"]
    assert result["protocolVersion"] == 1
    assert result["agentInfo"]["name"] == "wren"
    assert result["agentCapabilities"]["loadSession"] is False
    assert result["authMethods"] == []


def test_a_session_needs_a_real_absolute_folder(tmp_path):
    client = Client([], tmp_path)
    assert client.call("session/new", {"cwd": "relative/path"})["error"]["code"] == -32602
    assert client.call("session/new", {"cwd": str(tmp_path / "missing")})["error"]["code"] == -32602
    assert client.call("session/new", {"cwd": str(tmp_path)})["result"]["sessionId"].startswith("wren-")


# --- A turn -------------------------------------------------------------------------


def test_a_reply_streams_as_chunks_and_the_response_says_why_it_stopped(tmp_path):
    client = Client(["Hello there"], tmp_path)
    client.new_session()
    before, response = client.read_until_response(client.prompt("hi"))

    chunks = [u["content"]["text"] for u in updates(before) if u["sessionUpdate"] == "agent_message_chunk"]
    assert "".join(chunks) == "Hello there"
    assert response["result"] == {"stopReason": "end_turn"}
    assert all(m["params"]["sessionId"] == client.session_id for m in before)


def test_a_tool_call_is_announced_then_updated_with_its_result(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    client = Client([asks(("t1", "read_file", {"path": "a.txt"})), "Done."], tmp_path)
    client.new_session()
    before, _ = client.read_until_response(client.prompt("read it"))

    started, finished = [u for u in updates(before) if u["sessionUpdate"].startswith("tool_call")]
    assert started == {
        "sessionUpdate": "tool_call",
        "toolCallId": "t1",
        "title": "Read a.txt",
        "kind": "read",
        "status": "in_progress",
        "rawInput": {"path": "a.txt"},
        "locations": [{"path": str(tmp_path / "a.txt")}],
    }
    assert finished["toolCallId"] == "t1" and finished["status"] == "completed"
    # The person sees the summary, not the whole file the model was given.
    assert finished["content"] == [{"type": "content", "content": {"type": "text", "text": "2 lines"}}]


def test_a_failed_tool_call_shows_the_error(tmp_path):
    client = Client([asks(("t1", "read_file", {"path": "nope.txt"})), "Hm."], tmp_path)
    client.new_session()
    before, _ = client.read_until_response(client.prompt("read it"))
    finished = updates(before)[-2]
    assert finished["status"] == "failed" and "No such file" in finished["content"][0]["content"]["text"]


def test_attached_files_reach_the_model_as_text(tmp_path):
    client = Client(["ok"], tmp_path)
    client.new_session()
    blocks = [
        {"type": "text", "text": "explain this"},
        {"type": "resource", "resource": {"uri": "file:///x.py", "text": "print(1)"}},
        {"type": "resource_link", "uri": "file:///y.py", "name": "y.py"},
    ]
    client.call("session/prompt", {"sessionId": client.session_id, "prompt": blocks})
    sent = client.history[0].text
    assert "explain this" in sent and "print(1)" in sent and "file:///y.py" in sent


def test_the_step_limit_is_reported_as_max_turn_requests(tmp_path):
    client = Client([asks(("t1", "list_files", {}))], tmp_path)
    client.new_session()
    client.server.sessions[client.session_id].agent.max_steps = 1
    assert client.call("session/prompt", {"sessionId": client.session_id, "prompt": [{"type": "text", "text": "go"}]})[
        "result"
    ] == {"stopReason": "max_turn_requests"}


# --- Permission -----------------------------------------------------------------------


def test_an_edit_asks_the_client_with_a_real_diff_and_happens_once_allowed(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    edit = {"path": "a.txt", "old_string": "one", "new_string": "1"}
    client = Client([asks(("t1", "edit_file", edit)), "Done."], tmp_path)
    client.new_session()
    prompt_id = client.prompt("change it")

    asked = client.answer_permission("allow")
    assert asked["toolCall"]["toolCallId"] == "t1" and asked["toolCall"]["title"] == "Edit a.txt"
    diff = {"type": "diff", "path": str(tmp_path / "a.txt"), "oldText": "one\ntwo\n", "newText": "1\ntwo\n"}
    assert asked["toolCall"]["content"] == [diff]
    assert [(o["optionId"], o["kind"]) for o in asked["options"]] == [
        ("allow", "allow_once"),
        ("always", "allow_always"),
        ("deny", "reject_once"),
    ]

    before, response = client.read_until_response(prompt_id)
    assert (tmp_path / "a.txt").read_text() == "1\ntwo\n"
    finished = [u for u in updates(before) if u["sessionUpdate"] == "tool_call_update"][0]
    assert finished["status"] == "completed" and finished["content"] == [diff]
    assert response["result"] == {"stopReason": "end_turn"}


def test_a_new_file_is_a_diff_from_nothing(tmp_path):
    client = Client([asks(("t1", "write_file", {"path": "new.txt", "content": "hi\n"})), "Done."], tmp_path)
    client.new_session()
    prompt_id = client.prompt("make it")
    asked = client.answer_permission("allow")
    assert asked["toolCall"]["content"][0]["oldText"] is None
    client.read_until_response(prompt_id)
    assert (tmp_path / "new.txt").read_text() == "hi\n"


def test_denying_stops_the_action_but_not_the_turn(tmp_path):
    client = Client([asks(("t1", "write_file", {"path": "new.txt", "content": "hi"})), "Understood."], tmp_path)
    client.new_session()
    prompt_id = client.prompt("make it")
    client.answer_permission("deny")
    before, response = client.read_until_response(prompt_id)

    assert not (tmp_path / "new.txt").exists()
    assert [u for u in updates(before) if u["sessionUpdate"] == "tool_call_update"][0]["status"] == "failed"
    assert response["result"] == {"stopReason": "end_turn"}  # the model got to reply
    assert history_problems(client.history) == []


def test_always_is_remembered_within_the_session(tmp_path):
    replies = [
        asks(("t1", "write_file", {"path": "a.txt", "content": "1"})),
        asks(("t2", "write_file", {"path": "b.txt", "content": "2"})),
        "Done.",
    ]
    client = Client(replies, tmp_path)
    client.new_session()
    prompt_id = client.prompt("make two")
    client.answer_permission("always")
    before, _ = client.read_until_response(prompt_id)

    assert not any(m.get("method") == "session/request_permission" for m in before)  # no second question
    assert (tmp_path / "a.txt").exists() and (tmp_path / "b.txt").exists()


def test_a_command_with_shell_syntax_is_not_offered_always(tmp_path):
    client = Client([asks(("t1", "shell", {"command": "echo a; echo b"})), "Done."], tmp_path)
    client.new_session()
    prompt_id = client.prompt("run it")
    asked = client.answer_permission("deny")
    assert [o["optionId"] for o in asked["options"]] == ["allow", "deny"]
    assert asked["toolCall"]["content"][0]["content"]["text"] == "echo a; echo b"
    client.read_until_response(prompt_id)


# --- Cancelling -------------------------------------------------------------------------


def test_cancelling_at_a_permission_prompt_ends_the_turn_and_changes_nothing(tmp_path):
    client = Client([asks(("t1", "write_file", {"path": "new.txt", "content": "hi"})), "never said"], tmp_path)
    client.new_session()
    prompt_id = client.prompt("make it")

    client.cancel_at_permission_prompt()
    _, response = client.read_until_response(prompt_id)

    assert response["result"] == {"stopReason": "cancelled"}
    assert not (tmp_path / "new.txt").exists()
    assert client.history[-1].text == INTERRUPTED_NOTE
    assert history_problems(client.history) == []


def test_a_cancel_ends_the_wait_even_if_the_client_never_answers_the_question(tmp_path):
    # Not every client follows the spec's "answer cancelled" rule (Toad, when
    # we looked, did not), and a cancel may arrive as a request, not a notification.
    client = Client([asks(("t1", "write_file", {"path": "new.txt", "content": "hi"})), "never said"], tmp_path)
    client.new_session()
    prompt_id = client.prompt("make it")
    client.wait_for_permission()  # ...and never answer it
    cancel_id = client.request("session/cancel", {"sessionId": client.session_id})

    before, response = client.read_until_response(prompt_id)
    assert response["result"] == {"stopReason": "cancelled"}
    assert any(m.get("id") == cancel_id and m.get("result") is None for m in before)  # the cancel was answered
    assert not (tmp_path / "new.txt").exists()
    assert history_problems(client.history) == []


@pytest.mark.skipif(os.name != "posix", reason="uses POSIX shell commands")
def test_cancelling_stops_a_running_command_promptly(tmp_path):
    client = Client([asks(("t1", "shell", {"command": "sleep 30"})), "never said"], tmp_path)
    client.new_session()
    prompt_id = client.prompt("wait")
    client.answer_permission("allow")
    client.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": client.session_id}})

    _, response = client.read_until_response(prompt_id)  # would time out if we waited for sleep
    assert response["result"] == {"stopReason": "cancelled"}
    assert "Stopped by the user" in client.history[2].tool_results[0].content
    assert history_problems(client.history) == []


def test_a_session_can_be_used_again_after_a_cancel(tmp_path):
    client = Client([asks(("t1", "write_file", {"path": "x", "content": "x"})), "second turn"], tmp_path)
    client.new_session()
    prompt_id = client.prompt("first")
    client.cancel_at_permission_prompt()
    client.read_until_response(prompt_id)

    _, response = client.read_until_response(client.prompt("again"))
    assert response["result"] == {"stopReason": "end_turn"}
    assert history_problems(client.history) == []


# --- When things go wrong -------------------------------------------------------------------


def test_protocol_errors_get_json_rpc_error_codes(tmp_path):
    client = Client([], tmp_path)
    assert client.call("no/such_method", {})["error"]["code"] == -32601
    assert client.call("session/prompt", {"sessionId": "nope", "prompt": []})["error"]["code"] == -32602
    client.send({"jsonrpc": "2.0", "method": "some/unknown_notification"})  # ignored, as the spec asks
    client._out.write("this is not json\n")
    client._out.flush()
    assert client.read()["error"]["code"] == -32700


def test_a_provider_failure_is_an_error_response_and_the_history_stays_valid(tmp_path):
    client = Client([], tmp_path)  # no replies: the fake provider raises
    client.new_session()
    _, response = client.read_until_response(client.prompt("hi"))
    assert response["error"]["code"] == -32603 and "ran out of scripted replies" in response["error"]["message"]
    assert history_problems(client.history) == []


# --- The real program -------------------------------------------------------------------------


def test_wren_acp_speaks_only_json_on_stdout(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("WREN_", "ANTHROPIC_"))}
    env["PYTHONPATH"] = str(SRC)
    process = subprocess.Popen(
        [sys.executable, "-m", "wren", "acp", "--provider", "fake"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )

    def send(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def read_response(id):
        while True:
            message = json.loads(process.stdout.readline())  # every line must be JSON
            if message.get("id") == id:
                return message

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
        assert read_response(1)["result"]["agentInfo"]["name"] == "wren"
        send({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(tmp_path), "mcpServers": []}})
        session_id = read_response(2)["result"]["sessionId"]
        prompt = {"sessionId": session_id, "prompt": [{"type": "text", "text": "hello wren"}]}
        send({"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": prompt})
        assert read_response(3)["result"] == {"stopReason": "end_turn"}
        process.stdin.close()  # the client hangs up; Wren should exit by itself
        assert process.wait(timeout=10) == 0
    finally:
        process.kill()
