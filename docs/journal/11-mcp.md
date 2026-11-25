# 11 · MCP: tools from other programs

*Step 11, part 1 of 3 · November 2026*

## What we built

Wren can now use tools that other programs provide. List an MCP server in `~/.wren/mcp.json`:

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}
    }
  }
}
```

Start Wren, and that server's tools sit beside Wren's own, named `mcp__github__create_issue` and so on. Every call asks first, with the model's reason in plain English and the exact arguments underneath:

```text
  ▸ File the crash we just reproduced as a bug.  MCP: create_issue on github
    Use the tool create_issue from the MCP server github
    Wren says: File the crash we just reproduced as a bug.
    That is Wren's description. What will actually run is:
    │ {
    │   "owner": "bensullivan2002",
    │   "repo": "wren",
    │   "title": "Crash when resuming an empty session"
    │ }
    Allow? [y]es / [n]o / [a]lways allow the create_issue tool from the MCP server github this session:
```

`/mcp` shows what started, what didn't and why, and where each server's log is. Your four decisions shaped it: local (stdio) servers only (D137), the official SDK (D138), one personal config file (D139), and a question for every call (D140).

The other two parts of step 11, subagents and hooks, come next, each in its own entry.

---

## Idea 1: what MCP is, and why it exists

Every agent harness has the same problem: people want it to use *their* things, such as GitHub, a database, a browser or a ticket tracker. Without a standard, each harness writes its own GitHub tool, each with its own bugs, and each service writes a plugin for each harness.

The **Model Context Protocol** turns that many-to-many problem into many-to-one. A service writes one *server* that describes its tools in a standard way. Any harness that speaks MCP, which today means Claude Code, Codex, Cursor and now Wren, can list those tools and call them. It's the same move as USB, or the Language Server Protocol that gave every editor "go to definition" for every language.

Underneath, it's ordinary JSON-RPC: requests with an `id`, responses with the same `id`. Over stdio, each message is one line of JSON on the server's stdin or stdout. The two calls Wren cares about:

```text
→ {"jsonrpc":"2.0","id":3,"method":"tools/list"}
← {"jsonrpc":"2.0","id":3,"result":{"tools":[{"name":"add","description":"Add two numbers.",
     "inputSchema":{"type":"object","properties":{"a":{"type":"integer"},"b":{"type":"integer"}},...}}]}}

→ {"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"add","arguments":{"a":2,"b":3}}}
← {"jsonrpc":"2.0","id":4,"result":{"content":[{"type":"text","text":"5"}],"isError":false}}
```

A tool is described exactly as Wren describes its own: a name, a description and a JSON Schema. That's no coincidence. The model APIs settled on that shape first, and MCP adopted it.

## Idea 2: the spec moved under our feet

Before writing anything, we read the current spec. It changed a lot on 28 July 2026:

- **No handshake.** The old spec (2025-11-25) opened every connection with `initialize`, which fixed the protocol version and capabilities for the session. The new one is stateless. Each request carries its version and capabilities in `_meta`, and a new `server/discover` request tells a client what a server supports.
- **No sessions**, no `ping`, and every result now carries a `resultType`.
- The old HTTP+SSE transport, sampling, roots and logging are all deprecated.

Most servers people actually run were written for the older version. So a client today has to try `server/discover`, and if the server doesn't understand it, fall back to `initialize`. That one fact decided D138 for you. A hand-written client would have meant writing and testing two protocol versions ourselves, and keeping up with the next change. The SDK does it in its `Client(mode="auto")`:

```text
version 2026-07-28 name='demo' ...
```

That's our demo server (`tests/mcp_demo_server.py`, built with the SDK), negotiated to the new version. The SDK's documentation says an older server is handled by the same code, falling back to `initialize`; we haven't tried one yet. The first real server we tried, the reference filesystem server 2.4.0, already speaks the new version, so it didn't test the fallback (see "Tried for real"). `/mcp` shows which version each server agreed to.

## Idea 3: an async library inside a synchronous program

The SDK is async-only: every call is `await client.call_tool(...)`, inside an event loop. Wren isn't async (D34): the agent loop calls `registry.run()` and waits. Making Wren async to talk to MCP would mean rewriting the agent, the tools and both front ends.

The bridge is a pattern worth knowing: **give the async code an event loop of its own, on its own thread, and talk to it through futures.** anyio, the library the SDK is built on, calls this a *blocking portal*:

```python
self._portal = self._portal_stack.enter_context(start_blocking_portal())
...
future = self._portal.start_task_soon(_call, client, tool, arguments, timeout)
```

`start_task_soon` schedules a coroutine on the portal's loop and immediately hands back a `concurrent.futures.Future`. That's the same kind of object a `ThreadPoolExecutor` gives you, and Wren's thread can wait on it like any other. All the `async` stays inside `mcp_servers.py`.

### The rule that shaped the file

The first experiment opened the client with one portal call and meant to close it with another. It failed:

```text
RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
```

An `async with Client(...)` block owns a *task group*: the background tasks that read the server's stdout and write its stdin. anyio's rule, called *structured concurrency*, is that a task group is opened and closed by the same task. Each portal call is a new task, so it broke the rule.

The fix is to make each server's whole life one task:

```python
async def _serve(self, config, status, errlog, ready):
    ...
    async with client:
        status.protocol_version = client.protocol_version
        status.tools = await _list_tools(client)
        self._clients[config.name] = client
        ready.set_result(None)
        await anyio.sleep_forever()  # until close() cancels this task
```

It opens the client, tells Wren's thread it's ready through a `Future`, then sleeps. When Wren exits, `close()` cancels the task, the sleep is interrupted, and the `async with` closes the client properly. That closes the server's stdin, waits, and terminates it if it doesn't leave. Tool calls are separate short tasks, which the SDK allows.

Because each server is its own task, they all start at once: a slow `npx` download doesn't hold up the others.

## Idea 4: the bug that wasn't a hang

One test hung for ever: a call that should time out after half a second. The code waited in short slices so that Escape could interrupt it:

```python
try:
    return future.result(timeout=POLL_SECONDS)
except concurrent.futures.TimeoutError:
    continue            # not finished yet: check Escape, wait again
except TimeoutError:
    raise McpCallError("didn't answer within ...")
```

That reads well, but since **Python 3.11, `concurrent.futures.TimeoutError` *is* the built-in `TimeoutError`**: the same class under two names. When the call really timed out, the future held a `TimeoutError`, and `result()` re-raised it. The first `except` took it for "not finished yet", and the loop spun for ever.

The fix asks a different question, *is it done?*, which can't be confused with *did it fail?*:

```python
while not concurrent.futures.wait([future], timeout=POLL_SECONDS).done:
    if should_stop():
        future.cancel()
        raise McpCallError("Stopped by the user before the server answered.")
try:
    return future.result()
except TimeoutError:  # anyio.fail_after, in _call
    ...
```

The start-up wait had the same flaw: a server whose handshake timed out would have been reported as "didn't start within 60 seconds". It's in D138 as a lesson. The general point is that when one exception type means two things, catch neither, and ask a question that has only one meaning.

## Idea 5: approving what a stranger's program will do

Wren's own tools know what they change, so they can ask a precise question: "Edit src/app.py" with a diff. An MCP tool is a program Wren didn't write. All Wren knows is what the server says, and the spec is blunt about that:

> clients MUST consider tool annotations to be untrusted unless they come from trusted servers.

A server can label a tool `readOnlyHint: true`, and nothing checks it. So, as you chose, every call asks (D140), in auto mode too, and none is ever `contained`.

Asking only helps if you can read the question (D83). `{"owner": "x", "repo": "y", "title": "..."}` is JSON for a program you didn't write. So Wren adds one argument to every MCP tool's schema (D141):

```python
_REASON_SCHEMA = {
    "type": "string",
    "description": (
        "One plain-English sentence for the user: what this call will do and why. "
        "It is shown when asking the user's permission, and is not sent to the tool."
    ),
}
```

The model fills it in. The sentence leads the question, labelled as the model's, with the real arguments underneath, exactly as the shell's `description` does. `parse()` takes it out before anything else, so the server never sees it:

```python
arguments = dict(args)
reason = arguments.pop(REASON, None)
```

Then the arguments are checked against the server's schema with `jsonschema` (D147), so you're never asked to approve a call the server would refuse. Try `mcp mcp__demo__add {"a": "x"}` with the fake provider: it's refused with `'a': 'x' is not of type 'integer'; 'b' is a required property`, and you're never asked.

One more thing to know. What a tool *returns* is untrusted too. A server that fetches a web page passes on whatever that page says, including text written to steer the model. That's not new: files and command output are the same. It's the reason every call asks, and why the model's reason is never presented as fact.

## Idea 6: a server's tools join Wren's, for the whole session

`McpTool` is a `Tool` like any other, so the registry gives it everything for free: the permission check, "always", turning failures into error results, the 50,000-character cap, and Escape. It differs from Wren's own tools in where its schema comes from. It arrives at runtime, so instead of a Pydantic `Input`, `McpTool` overrides `spec()` and `parse()`.

The tool list is part of every request, and changing it mid-conversation would break the prompt cache (invariant 16). So servers start before the first prompt, and the list is fixed from then on (D143). A server that crashes leaves its tools listed, and calls to them say it isn't running.

## Idea 7: keeping a server's noise off your screen

Servers write diagnostics to stderr, and the SDK logs through Python's `logging`, which with no handler set up prints to stderr too. In the full-screen app either would draw over the conversation. So each server's stderr goes to `~/.wren/logs/mcp-NAME.log`, and the SDK's logger gets a file handler and stops propagating (D144). The startup notice and `/mcp` tell you where to look when a server won't start.

## Idea 8: what a server can see

A server is a program you chose, running with your permissions and outside the sandbox: it can do anything you can. Two things limit what it's *given*:

- **Environment.** The SDK passes only `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` and `USER`, plus what you list under `env`. Your `ANTHROPIC_API_KEY` doesn't reach it unless you put it there. `${GITHUB_TOKEN}` lets the file name a secret without containing it.
- **What's sent.** Only the arguments of calls you approved.

That's also why there's no project `.mcp.json` yet (D139). Reading one would let any repository you clone choose programs for Wren to run.

---

## Python worth knowing from this step

- **`concurrent.futures.Future`** is a box for a result that isn't ready yet: `done()`, `result()`, `exception()`, `cancel()`. You can make one yourself (`ready = Future()`) and fill it from another thread with `set_result` or `set_exception`, which is how `_serve` tells `start()` a server is up.
- **`concurrent.futures.wait(futures, timeout=…)`** returns `(done, not_done)` without raising, which makes it the safe way to poll.
- **`contextlib.ExitStack`** collects things to close, and closes them in reverse order. `McpServers` keeps two, so the server tasks are stopped *before* their log files close.
- **`typing.Protocol`** (`Caller` in `mcp_tool.py`): "anything with a `call` method like this". `McpServers` and the tests' `FakeCaller` both fit, without sharing a base class.
- **Exception groups.** A task group that sees several failures raises them together, as an `ExceptionGroup`, whose own message is just "unhandled errors in a TaskGroup". `describe()` unwraps single-member groups to find the real error, checking the shape rather than the class, because `ExceptionGroup` is built in only from Python 3.11.
- **`os.kill(pid, 0)`** sends no signal, but fails if the process doesn't exist: the test uses it to prove a server was stopped.

## Tests

`tests/test_mcp.py` has 37 tests. Most use a fake `Caller`, so they need no server. They cover the config file (variables, every kind of bad entry), the schema the model sees, the permission question, declining, "always" for one tool, auto mode still asking, argument checks happening before the question, result rendering, `/mcp`, and a full agent turn that leaves a valid history. Seven start `tests/mcp_demo_server.py`, a real stdio server built with the SDK's own server side. Those cover start-up, a call, a failing tool, Escape, a timeout, a server that can't start beside one that can, and a process that's really gone after `close()`.

## Things to try

1. **Offline, with the demo server.** Put this in `~/.wren/mcp.json` (use your real path to the repo), then `wren --provider fake`:
   ```json
   {"mcpServers": {"demo": {"command": "/path/to/wren/.venv/bin/python",
                            "args": ["/path/to/wren/tests/mcp_demo_server.py"]}}}
   ```
   Then try `/mcp`, `/tools mcp__demo__add`, `mcp mcp__demo__add {"a": 2, "b": 3}`, a bad call such as `mcp mcp__demo__add {"a": "x"}`, and `mcp mcp__demo__slow {"seconds": 30}`, then press Escape.
2. **A real server, with a real model.** The reference filesystem server is a good first one: `"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]`. Ask the model to list `/tmp`, and see whether its `wren_reason` sentences are good enough to approve without reading the JSON.
3. **Break it on purpose.** Misspell `args` as `arg`, use `${SOMETHING_UNSET}`, point `command` at nothing, and see what the startup notice and `/mcp` say.
4. **Watch the protocol.** A server's log file holds whatever it writes to stderr. Some servers log every request.

## Tried for real

*25 November 2026 · the reference filesystem server 2.4.0 (`npx -y @modelcontextprotocol/server-filesystem /tmp`) · Claude Opus 5.5, high effort · the plain REPL, with answers piped in*

This is "Things to try" 2. Two runs, each a fresh Wren:

- **Start-up and `/mcp`.** The server started and offered 16 tools. It agreed protocol `2026-07-28`, the new version, so the fallback to `initialize` (Idea 2) is still untried.
- **Declining.** Asked to list `/tmp`, the model chose `list_directory_with_sizes`. The call was declined, never reached the server, and the model said what it had meant to do and asked how to go on.
- **Approving.** Asked to create a folder, write a file, change one line and read it back, the model made five calls. Four answers of "yes" were piped in, so the first four calls ran. The fifth was declined because the input had run out. The file on disk was right, and the edit's diff came back as the result.

The point of the exercise was the `wren_reason` sentences. These are all six, each with the arguments that were shown under it:

```text
List the contents of /tmp with file sizes, largest first, to find the biggest items.
    {"path": "/tmp", "sortBy": "size"}
Check which directories the filesystem server can access before creating /tmp/wren-mcp-test.
    (no arguments)
Create the test folder /tmp/wren-mcp-test as requested.
    {"path": "/tmp/wren-mcp-test"}
Write notes.txt with three short lines about birds.
    {"path": "/tmp/wren-mcp-test/notes.txt", "content": "Robins often sing at dawn.\n..."}
Replace the second line of notes.txt with a line about wrens.
    {"path": "...", "edits": [{"oldText": "Sparrows gather in noisy flocks.", "newText": "Wrens are tiny birds..."}]}
Read notes.txt back to show the user the final contents.
    {"path": "/tmp/wren-mcp-test/notes.txt"}
```

**Verdict: good enough to approve by.** Every sentence was accurate, matched its arguments, and was readable without the JSON. Three weak spots:

- **A file without its folder.** "Write notes.txt" doesn't say where. For a write, the full path is the thing most worth checking, so it should be in the sentence.
- **"As requested" as the why.** That's true, but it adds nothing. It's fine when the call is exactly what you asked for. It would hide a call that drifts from what you asked.
- **A small over-promise.** "Largest first" isn't in `{"sortBy": "size"}`. The server may well sort that way, but the sentence claims more than the arguments show.

If these matter, the place to fix them is the wording of `_REASON_SCHEMA` (Idea 5), for example by asking for full paths. One more thing worth knowing: the model also took a step nobody asked for, checking which folders it was allowed to use. Each call costs you a question, so extra steps like that have a price.

Not tried: "always" for an MCP tool, and an older server. The config was removed afterwards. The server mostly duplicates Wren's own file tools, and an unpinned `npx -y` can fetch new code from npm at every start.

## Known limits

- **Local servers only** (D137): no HTTP, no OAuth.
- **Personal config only** (D139): no project `.mcp.json`.
- **Tools are fixed at start-up** (D143): `list_changed` is ignored, and a crashed server isn't restarted.
- **Text only** (D145): images and audio are described, not shown to the model.
- **No server `instructions`**: some servers send guidance for the model when they connect. Wren doesn't add it to the system prompt yet. It would need to be treated as untrusted, like memory files.
- **Only tools.** MCP also defines resources and prompts; Wren uses neither.
- **Input requests aren't answered.** The new spec lets a server ask the user a question mid-call (elicitation). Wren gives the SDK no handler for it, so such a call can't complete. What exactly the model sees then is untested.
- **Long tool descriptions cost tokens on every request**, like any tool's, and some servers' descriptions are long. `/tools NAME` shows exactly what's sent.

## What's next

The other two parts of step 11: **subagents** (a task handed to a fresh conversation) and **hooks** (your own commands at set moments). Each starts with its own questions.
