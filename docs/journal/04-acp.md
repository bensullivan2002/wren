# 04 · Borrowing a UI: Wren speaks ACP

*Step 4 · October 2026*

## What we built

The plan for step 4 was "a better terminal UI". We ended up not writing one. Wren learned a protocol, and now a UI someone else wrote can drive it.

```bash
wren acp          # speak the Agent Client Protocol on stdin and stdout
```

Run by hand, that command just sits there waiting for JSON. It's meant to be started by a **client**: a terminal UI such as [Toad](https://github.com/batrachianai/toad), or an editor. The client draws the interface (markdown, diffs, permission dialogs, a prompt box), and Wren does what it already did.

```bash
uv tool install -U batrachian-toad --python 3.14      # Toad: macOS or Linux
cd ~/some/project
toad acp "/full/path/to/wren/.venv/bin/wren acp"      # with ANTHROPIC_API_KEY set
toad acp "/full/path/to/wren/.venv/bin/wren acp --provider fake"   # or free, with the pretend model
```

`wren` on its own still starts the plain REPL from steps 1 to 3. Wren now has two front ends, and that is the real subject of this entry.

> **Not yet checked against a real client.** Everything here is tested by a test client that sends the same bytes Toad would, built from the ACP spec and a reading of Toad's source. It has not been run under Toad itself. Expect a rough edge or two on first contact; see "Known limits".

---

## How we got here

Worth recording, because the route shows the trade-offs:

1. The first recommendation was to build our own inline UI with Rich and prompt_toolkit.
2. You remembered "something built on Textual". That was Toad: not a library but a finished app, by the author of Rich and Textual, that fronts *any* agent speaking ACP.
3. You chose a full-screen Textual app of our own, which raised a hard question: Textual is async, Wren's agent is synchronous. Join them with threads, or rewrite the core?
4. You went back to Toad. The async question mostly dissolved, the UI work disappeared, and what was left was a protocol.

The cost of this choice is real: you won't learn how streaming markdown or a spinner works, and Wren's own REPL stays plain. What you get is a polished UI now, and a look at how tools in this ecosystem plug together.

## Idea 1: the M×N problem, and why protocols exist

Suppose there are M coding agents and N places you'd like to use one (terminals, editors, web UIs). Without a standard, every pairing needs its own integration: M×N pieces of glue. With a shared protocol, each agent implements it once and each client implements it once: M+N.

That is exactly the story of the **Language Server Protocol**, which is why your editor understands Python, Rust and Go without containing a compiler for each. The **Agent Client Protocol** is the same move for agents, and it borrows LSP's shape closely:

- the client starts the agent as a **child process**;
- they talk over the child's **stdin and stdout**;
- every message is **JSON-RPC**, one per line.

Step 8's MCP is the third member of the family: a protocol between an agent and its *tools*. ACP is between an agent and its *user interface*.

## Idea 2: JSON-RPC is three message shapes

`acp/jsonrpc.py` is the whole wire layer, under 200 lines, and the protocol itself fits in a docstring:

```
a request        {"jsonrpc": "2.0", "id": 7, "method": "session/new", "params": {...}}
its response     {"jsonrpc": "2.0", "id": 7, "result": {...}}
              or {"jsonrpc": "2.0", "id": 7, "error": {"code": -32602, "message": "..."}}
a notification   {"jsonrpc": "2.0", "method": "session/cancel", "params": {...}}
```

A request has an `id` and must be answered with the same id. A notification has no id and gets no answer. You can watch it work with nothing but a shell:

```bash
echo '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":1}}' | wren acp --provider fake
```

## Idea 3: it runs both ways, so replies need filing

Here's what makes it more than "read a line, answer a line". In the middle of handling the client's `session/prompt`, Wren needs to *ask the client* something: "may I edit this file?" So Wren sends its own request and waits. But the next line to arrive on stdin might not be the answer. It might be a cancel.

So "wait for the reply" can't mean "read the next line". Instead, every request we send is filed under its id, with a little mailbox (a `queue.Queue`) for the answer:

```python
answer: queue.Queue = queue.Queue(maxsize=1)
with self._waiting_lock:
    self._next_id += 1
    id = self._next_id
    self._waiting[id] = answer                      # file it
self._write({"jsonrpc": "2.0", "id": id, "method": method, "params": params})
...
message = answer.get(timeout=0.1)                   # sleep until someone delivers
```

One thread does all the reading. When it sees a response, it looks up the id and drops the message in that mailbox, which wakes the waiting thread. This pattern (correlating replies to requests by id) is in every RPC system you'll ever use; it's usually hidden inside a library.

## Idea 4: two threads, because a turn is slow

A turn can take minutes. During it, stdin must keep being read: that's where a cancel arrives, and where permission answers arrive. One thread can't both run the turn and read stdin. So:

| Thread | Does |
| --- | --- |
| main | reads stdin; answers quick requests (`initialize`, `session/new`); delivers responses; sets the cancel flag |
| one per `session/prompt` | runs the agent's turn; sends updates; waits for permission answers |

Both write to stdout, so `Connection._write` takes a lock: each message goes out whole, never spliced into another.

The agent itself didn't become async, and didn't change at all. The threads live entirely inside `acp/`.

## Idea 5: the server is only a translator

Read the table at the top of `acp/server.py`. It is the design:

```
Wren                                 ACP
----------------------------------   ------------------------------------
TextDelta                        ->  session/update  agent_message_chunk
ToolStarted                      ->  session/update  tool_call
ToolFinished                     ->  session/update  tool_call_update
Permissions.ask(request)         ->  session/request_permission (and wait)
closing the turn's generator     <-  session/cancel
TurnFinished / how it ended      ->  the session/prompt response
```

Compare `AcpServer._report` with `Repl.show`. They are the same function aimed at different targets: one prints, one sends JSON. And `AcpServer._ask_permission` is `Repl.ask_permission` with a network round trip where `input()` was.

This is the payoff for two rules we've kept since step 1: **the agent yields events and never prints**, and (from step 3) **permission is a callback, not a `print`/`input` pair buried in a tool**. At the time those looked like tidiness. They were the seam a second front end plugs into. `agent.py` has no changes in this step.

## Idea 6: cancelling is closing the generator

In the REPL, Ctrl-C raises `KeyboardInterrupt`. A network client can't raise an exception in our process; it sends `session/cancel`. The reading thread sets a flag, and the turn's loop notices:

```python
with closing(session.agent.run_turn(text)) as events:
    for event in events:
        if session.cancelled.is_set():
            break                                   # leaving the block closes the generator
        self._report(session, event)
return {"stopReason": "cancelled" if session.cancelled.is_set() else stop_reason}
```

Leaving the `with closing(...)` block calls the generator's `close()`, which raises `GeneratorExit` inside the agent at its paused `yield`, which runs step 3's `_close_unfinished_turn`. So a cancel from Toad tidies the history exactly as Ctrl-C does. We built that for one front end and the second got it for free.

This is **cooperative** cancellation: the turn stops when it next looks at the flag. That's immediate while text streams. For the slow tool, the shell, we added a way to look more often (next idea). The one wait we can't cut short is for the model's first words, when the thread is blocked inside the network call.

## Idea 7: what the tools layer had to learn (two small things)

ACP has its own vocabulary, and none of it reached the tools. But two *neutral* gaps showed up, things any richer front end would want:

**A change, as data.** The REPL shows a diff as text. Toad wants the old and new contents, to draw the diff itself. So edits now carry a `FileChange(path, before, after)` on both the `PermissionRequest` and the `ToolResult`. The REPL ignores it; ACP sends it as a `diff` block.

**A way to say "stop".** `ToolContext.should_stop` is a function tools can call. By default it always says no. The shell now polls it five times a second while a command runs:

```python
output, _ = process.communicate(timeout=POLL_SECONDS)      # wait a moment...
...
stopped = "cancelled" if should_stop() else "timeout" if time.monotonic() >= deadline else None
```

ACP wires `should_stop` to the session's cancel flag. There's a test that cancels a `sleep 30` and expects an answer at once.

The test of whether an addition belongs in the tools layer: would it make sense if ACP didn't exist? Both of these would.

## Idea 8: stdout is sacred

In ACP mode, stdout *is* the protocol. One stray `print()`, ours or a library's, lands in the middle of the JSON stream and the client chokes. So `run_acp` does this before anything else:

```python
protocol_out, sys.stdout = sys.stdout, sys.stderr
```

The real stdout is kept for the connection, and `sys.stdout` now points at stderr. Any `print` anywhere goes somewhere harmless. A test starts the real program and requires every line of its output to parse as JSON.

## Idea 9: be strict in what you send, liberal in what you accept

Reading Toad's client code before shipping turned up two places where it didn't do what the spec says a client must:

- The spec makes `session/cancel` a notification. Toad appeared to send it as a request, and wait for an answer.
- The spec says a client must answer an open permission question with "cancelled" when the user cancels. Toad appeared not to.

Either would have left Wren stuck: a cancel that's rejected as "unknown method", or a turn waiting forever on an answer that's never coming. So Wren accepts a cancel either way (and answers it if it had an id), and a cancel ends the permission wait from *our* side (`give_up=session.cancelled`).

This is an old piece of internet engineering advice (Postel's law). A spec tells you what you may send. Only the other side's actual behaviour tells you what you'll receive.

## Idea 10: showing people less than the model

When a tool finishes, the model gets all of `result.content`. A person watching wants the gist. `_result_content` picks, per your choice of "compact, with key detail":

| Tool | The client is sent |
| --- | --- |
| `edit_file`, `write_file` | the diff |
| `shell` | the command's output (the last 4,000 characters) |
| reads and searches | the one-line summary: `2 lines`, `7 matches in 3 files` |
| anything that failed | the error |

## What we chose not to use

ACP lets an agent ask the *client* to read and write files and run terminals (`fs/read_text_file`, `terminal/create`…), so an editor can serve unsaved buffers and show live terminals. Wren ignores these and runs its own tools. Our fence, our permission checks and our tests all live in those tools; delegating would mean a second implementation of each. The cost: an editor's unsaved changes are invisible to Wren.

---

## Python worth knowing from this step

- **`threading.Event`** is a flag one thread sets and another checks (`is_set()`), safely. It's how a cancel crosses from the reading thread to the turn.
- **`threading.Lock`** with `with`: only one thread at a time runs the block. Used so messages don't interleave. `lock.acquire(blocking=False)` *tries* to take it and returns False if it's busy, which is how a session refuses a second simultaneous turn.
- **`queue.Queue`** is a thread-safe mailbox. `get()` sleeps until something is `put()`. `get(timeout=0.1)` raises `queue.Empty` if nothing came, letting us look at the cancel flag between naps.
- **Daemon threads** (`daemon=True`) don't keep the program alive when the main thread ends.
- **`os.pipe()`** makes a connected pair of file descriptors. The tests use two pairs as a fake stdin and stdout, so the server runs for real with no subprocess.
- **A function as a dataclass default.** `should_stop: Callable[[], bool] = lambda: False`.
- **Nested functions as factories.** `make_agent` in `cli.py` is defined inside `main` so it can use `provider` and `config` without them being passed around: a *closure*.

## Known limits (on the list for later)

- **Untested against real Toad** (see the note at the top). First contact may need fixes.
- **Cancel can lag** while waiting for the model's first words.
- **No saved sessions** (`loadSession: false`): a client can't resume a conversation. That's roadmap step 7.
- **No modes, plans, slash commands or usage updates.** ACP has messages for all of them; Wren sends none yet.
- **Text only.** Images and audio in prompts are declined in `initialize`.
- **`mcpServers` in `session/new` is ignored** until step 8.
- **The plain REPL didn't get better.** No markdown rendering or spinner there.

## Things to try

1. Run the `echo … | wren acp --provider fake` line above. Then add a second line with `session/new` (it needs `"cwd"`, an absolute path).
2. Read `tests/test_acp.py`: the `Client` class is a tiny ACP client, and each test is a conversation.
3. In Toad with `--provider fake`: `write notes.txt hello`, and compare the permission dialog to the REPL's. Same `PermissionRequest`, two drawings.
4. In Toad: `run sleep 60`, allow it, then cancel. Then ask anything, and check the session still works.
5. Put a `print("oops")` at the top of `AcpServer.initialize` and run the `echo … | wren acp` line with `2>/dev/null` on the end. Where did "oops" go? Now also comment out the stdout swap in `run_acp` and run the tests: which one catches it?
6. Compare `Repl.show` and `AcpServer._report` side by side.

## What's next: step 5, context management

Every request re-sends the whole conversation, and tool results make conversations grow fast. Step 5 is about that: counting tokens, a budget, compacting old history, and prompt caching so the unchanged prefix is cheap. It plugs in inside `Agent`, just before each call to the provider, and (with `usage_update`) gives ACP clients a context meter to show.
