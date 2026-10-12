# Decisions

A log of the significant choices behind Wren: what we chose, what we didn't, and why. Newest last. When a decision is reversed, it stays here, marked as superseded, with a pointer to what replaced it.

Each entry notes who made the call: **you** (the project owner, asked during the build) or **design** (made while building, open to challenge).

---

### D1. Wren is a coding agent
*Step 1 · you*

Wren's job is to work on code: read it, search it, change it, and run commands. This is the classic harness, the shape of tools like Claude Code, and it exercises every hard part: tools, permissions, long contexts.
*Not chosen:* a general-purpose task agent; a bare framework with no built-in tools.

### D2. Written in Python
*Step 1 · you*

Python is the most readable choice for a project whose main goal is to be understood, and Anthropic's Python SDK is first-class.
*Not chosen:* TypeScript (what Claude Code uses), Go, Rust.
*Cost:* slower startup than a compiled binary, and users need Python installed.

### D3. We own the agent loop
*Step 1 · you*

We call the plain Messages API and write the loop, tool handling and context management ourselves, rather than building on a framework that runs the loop for us. Slower to build, but nothing is a black box.
*Not chosen:* the Claude Agent SDK; raw HTTP with no SDK at all.

### D4. Claude first, swappable later
*Step 1 · you*

Wren targets Claude through the Anthropic API, but only behind a small `Provider` interface, so other providers can be added without touching the rest of the code.
*Not chosen:* multi-provider from day one; local models only.

### D5. Wren has its own message types
*Step 1 · design*

The agent works with Wren's own small dataclasses (`messages.py`), never with the Anthropic SDK's types. Each provider translates at its edge. This is what makes D4 real: if Anthropic types leaked into the agent, "swappable" would mean "rewrite".
*Cost:* translation code in each provider, and the neutral types can only express what all providers can do.

### D6. Messages are lists of blocks, from the start
*Step 1 · design*

A message's content is a list of typed blocks (`TextBlock`, `ToolUseBlock`, `ToolResultBlock`), even though step 1 only uses text. Changing the shape of the core data later would touch every file, so we set it now.

### D7. Streaming from day one
*Step 1 · design*

Providers yield events as the model writes, rather than returning a finished reply. Replies feel faster, and the interface shape (an iterator of events ending in `Completed`) is set before anything depends on a non-streaming one.

### D8. A fake provider
*Step 1 · design*

`FakeProvider` implements the same interface with scripted or echoed replies. Tests use it, so they need no network or API key, and anyone can try Wren offline with `--provider fake`.

### D9. Interrupted turns are rolled back
*Step 1 · design* · **Superseded by D24 in step 3.**

If a turn is interrupted or fails, the agent removes that turn's messages entirely. The alternative, keeping a partial reply, means deciding what "half a turn" means, and once tools arrive a half-finished turn can be outright invalid for the API.

### D10. Standard library only for the terminal, for now
*Step 1 · design*

The REPL uses `input()`, `readline` and raw ANSI colour codes: no UI libraries. It keeps step 1 small and shows what's really happening. A richer interface is step 4, and that's a decision for you then.

### D11. Default model: `claude-sonnet-5-5`
*Step 1 · design*

Sonnet is strong at coding and cheaper and faster than Opus, which matters for an agent that makes many calls per task. Change it any time with `--model` or `WREN_MODEL`.

### D12. Docs live in the repo, as Markdown
*Step 1 · design*

The docs version with the code, so they can't drift apart unnoticed, and they read well on GitHub (including diagrams).

---

### D13. Hand-written tool schemas now, Pydantic in step 3
*Step 2 · you* · **Carried out in step 3:** see D25.

In step 2, each tool's JSON Schema is written out by hand and checked by a small hand-rolled validator (`tools/validation.py`). You see exactly what the model sees and what validation involves. At the start of step 3, before any tool can change files, we refactor to one Pydantic model per tool, which generates the schema and validates input from a single definition.
*Why not straight to Pydantic:* learning first, then the better-engineered version. The refactor is cheap because nothing outside each tool touches its schema.
*Not chosen:* decorators that infer schemas from function signatures (terser, more magic; underneath, they build the same kind of model).

### D14. Dedicated tools first; a shell tool in step 3
*Step 2 · you*

Reading, listing and searching are separate, narrow tools. The industry norm is dedicated tools *plus* a shell tool for everything else, gated by permissions, so a shell arrives in step 3.
*Why:* dedicated tools are easy to judge as safe, their output can be shaped for the model, and they work on every OS. The shell's strength is reach, which only matters once Wren can act.

### D15. The loop continues while replies contain tool requests
*Step 2 · design*

The agent checks the reply's content for `tool_use` blocks rather than relying on `stop_reason == "tool_use"`. A reply cut off by `max_tokens` can still contain a request, and every request must be answered.

### D16. A step limit per turn, ended with a note
*Step 2 · design*

At most 25 model calls per turn by default (`--max-steps`). When the limit hits, the agent appends a bracketed note in the assistant's place, so the history stays valid and the model knows why it stopped. The user can say "continue".
*Not chosen:* a final call with tools disabled to force a summary (one more request, and a provider feature to depend on).

### D17. Tools are confined to the working directory
*Step 2 · design*

Every path is resolved (collapsing `..` and following symlinks) and must land inside the folder Wren was started in. Read-only access to `~/.ssh` would still be a leak.

### D18. Tool failures are results, not exceptions
*Step 2 · design*

The registry turns every failure (an unknown tool, bad arguments, a `ToolError`, even a bug) into a result marked `is_error`, written for the model to act on. The loop never crashes because of a tool; only Ctrl-C escapes.

### D19. Tool output is capped and paged
*Step 2 · design*

Each tool limits its own output (500 lines per read, 200 listing entries, 100 search matches) and says how to get more. The registry caps every result at 50,000 characters as a safety net. Results are re-sent with every later request, so size is a recurring cost.

### D20. Pure-Python file tools, with a fixed ignore list
*Step 2 · design*

`search` uses Python's `re`, `list_files` uses `os.walk` with a small glob translator, and both skip a fixed set of folders (`.git`, `node_modules`, `.venv`…). No external programs, so it works anywhere Python does.
*Cost:* slower than ripgrep on big projects; `.gitignore` isn't honoured yet.

### D21. Tools run one at a time
*Step 2 · design*

When the model asks for several tools in one reply, they run sequentially, in order, and the results go back together. Running them in parallel is faster but complicates output and, once tools have side effects, ordering. Simple first.

---

### D22. Our own edit and shell tools, not Anthropic's predefined ones
*Step 3 · you*

`edit_file`, `write_file` and `shell` are ordinary Wren tools with Pydantic inputs. Anthropic's API also offers predefined, schema-less tool types (`text_editor_20250728`, `bash_20250124`) that Claude is trained on.
*Why ours:* they work with any provider (D4, D5), they go through the same schema and validation as every other tool (D25), and every line is visible. It's also the industry norm: Claude Code's own Edit, Write and Bash are custom tools of this shape.
*Not chosen:* the predefined types (Anthropic-only; no schema to own; the editor's `view` command duplicates `read_file`; `bash` expects a persistent session). Our own tools copying the predefined editor's multi-command shape (one tool with four commands is harder to describe and to ask permission for than separate narrow tools).
*Cost:* we write and maintain the descriptions.

### D23. Reads are free; edits and commands ask; "always" lasts for the session
*Step 3 · you*

Read-only tools never ask. Edits and commands ask every time, with three answers: yes, no, or always. "Always" covers all file edits, or shell commands starting with the same word, and is held in memory until Wren exits.
*Not chosen:* asking every time with no "always" (safest, but prompts that appear too often stop being read). Saving "always" rules to a project file (the full industry standard, as in Claude Code; left for a later step because it needs a config format and rule matching, which is more to get wrong).

### D24. A turn that's cut short is kept, not rolled back
*Step 3 · you · supersedes D9*

After Ctrl-C or an error, the history keeps everything that happened. Unanswered tool requests get a result (the real one, "cut short", or "not run"), and a closing assistant note records where the turn stopped. One rule covers interrupts and errors alike.
*Why:* once tools have side effects, deleting a turn from the history doesn't undo it. The model would forget changes that are really on disk.
*Not chosen:* rolling back only when nothing but read-only tools ran (two behaviours to understand and test). Snapshotting files and undoing edits on interrupt (shell commands can't be undone, so the promise would be partial; better as a separate undo feature).
*Cost:* a turn that fails on its very first model call still leaves your message and a note in the history, where D9 left nothing.

### D25. Tool inputs are strict, closed Pydantic models
*Step 3 · design*

Every tool's `Input` inherits `ToolInput`, which sets `strict=True` (no type conversion: `"10"` is not `10`) and `extra="forbid"` (unknown arguments are errors). Pydantic's errors are rewritten into sentences for the model. Field titles are stripped from the generated schema to save tokens. `pydantic>=2` is now a declared dependency.
*Not chosen:* Pydantic's default lax mode, which would quietly accept `"10"`. It's more forgiving, but a wrong type means the model misread the schema, and a clear error teaches it faster than a guess. Open to challenge if real use shows the model tripping on it.

### D26. Tools describe their effects; the registry enforces; the front end asks through a callback
*Step 3 · design*

A tool returns a `PermissionRequest` from `permission()`, or `None` if it only reads. `ToolRegistry.run` asks `Permissions.allows` between validation and running. `Permissions` asks the user through a callback that `cli.py` points at the REPL. With no callback, everything that needs permission is refused (fail closed).
*Not chosen:* a central table of which tool names need permission (the knowledge of what a call will do belongs with the tool, and a table can't show a diff). A `PermissionRequested` event answered with `generator.send()` (purer, since the agent would stay the only channel to the UI, but it threads a two-way protocol through every generator between the registry and the REPL).

### D27. `edit_file` replaces exact text that must be unique; `write_file` is separate
*Step 3 · design*

An edit names the text to replace. It fails if that text is missing, or occurs more than once without `replace_all`. Creating and overwriting files is a different tool.
*Why:* a stale picture of the file makes the edit fail loudly instead of applying wrongly.
*Not chosen:* whole-file rewrites only (expensive, and lossy on long files), line-number edits (numbers go stale), unified-diff patches (models get the line counts in hunk headers wrong).

### D28. Shell "always" is keyed on the first word, for simple commands only
*Step 3 · design*

"Always allow" for a command covers later commands with the same first word, and is only offered when the command contains no shell syntax (`;`, `&`, `|`, `$`, redirects, brackets, backslashes, line breaks) and no leading `VAR=value`. Anything else is asked about every time.
*Why:* `pytest; rm -rf ~` also starts with `pytest`. The check doesn't try to parse quoting, so it sometimes asks when it needn't; it never fails to ask when it should.
*Cost:* the first word is a coarse unit. "Always allow `git`" includes `git push --force`.

### D29. The shell runs each command fresh, with a timeout, and is not sandboxed
*Step 3 · design*

Each command gets a new shell in the working directory, no keyboard input, combined stdout and stderr, and a timeout (120 seconds by default, 600 at most), after which its whole process group is killed. A non-zero exit code is reported in a normal result, not an error. Long output keeps its tail.
*Not chosen:* one persistent shell session (state carries over, but so do surprises, and it's much more machinery). Sandboxing (the right long-term answer; a project of its own).
*Cost:* the shell can reach anything you can. The permission prompt is the only protection.

### D30. Writing tools never touch `.git`
*Step 3 · design*

`edit_file` and `write_file` refuse any path inside `.git`. Its hooks are scripts git runs by itself, so writing there would be a way to run code later with nobody approving it, and "always allow edits" would cover it.

### D31. A declined action is an error result, and the turn goes on
*Step 3 · design*

Saying no doesn't end the turn. The model gets a result saying the user declined, not to retry or work around it, and to ask how to proceed.
*Not chosen:* ending the turn on a no (simpler, but the model never learns it was declined, and may ask again next turn).

---

### D32. The better UI is someone else's: Wren speaks ACP, and Toad draws it
*Step 4 · you*

Step 4 was planned as "a better terminal UI". Instead, Wren gained an ACP mode (`wren acp`), so that Toad, or any other ACP client, is its interface.
*The route:* the first recommendation was our own inline UI on Rich and prompt_toolkit. You then chose a full-screen Textual app of our own, and after seeing what that involved (Textual is async, the agent is not) chose Toad.
*Not chosen:* Rich + prompt_toolkit (Aider's approach: the most to learn about terminal UIs for the least machinery). Our own Textual app (highest ceiling, largest rewrite). Rich alone.
*Cost:* nothing was learned about building a terminal UI; Wren's own REPL is unchanged and plain; the polished experience depends on a young third-party app that needs macOS or Linux.

### D33. The ACP layer is hand-written, on the standard library
*Step 4 · you*

`acp/jsonrpc.py` and `acp/server.py` implement the wire and the protocol directly: no new dependency, and every message is visible.
*Not chosen:* the official `agent-client-protocol` Python SDK (typed models, kept current with the spec; but its base classes are async, so the synchronous agent would need bridging, and the wire becomes a black box).
*Cost:* we track spec changes ourselves, and implement only what Wren needs.

### D34. Threads, not async: one reader, one thread per turn
*Step 4 · design*

The agent stays synchronous. The main thread reads stdin; each `session/prompt` runs on its own thread. Cancelling is cooperative: a flag the turn checks at each event, and the shell polls.
*Not chosen:* rewriting the agent, providers and tools as async (how most production agents are built; cancellation would be immediate everywhere, but every core file and most tests would change).
*Cost:* a cancel can lag while the model is producing its first words. This question was put to you during the Textual plan and left open when you changed course; it's a design call here, and worth revisiting if parallel tool calls or subagents (step 8) need it.

### D35. Wren keeps running its own tools under ACP
*Step 4 · design*

ACP lets an agent delegate file access and terminals to the client. Wren doesn't: the working-directory fence, permission checks and tests live in Wren's tools, and delegating would mean a second implementation of each.
*Cost:* unsaved changes in an editor's buffers are invisible to Wren, and commands don't appear in the client's own terminal panes.

### D36. The REPL stays, as a second front end
*Step 4 · design*

`wren` with no mode still starts the plain REPL. It needs no other program, it's what the end-to-end tests drive, and having two front ends keeps the boundary between agent and interface honest.

### D37. Front-end vocabulary stays out of the core; two neutral hooks were added
*Step 4 · design*

ACP's terms (tool kinds, titles, update types) live only in `acp/`. The tools layer gained two things any front end could use: `FileChange` (an edit as before-and-after text, on `PermissionRequest` and `ToolResult`) and `ToolContext.should_stop` (so a long-running tool can be stopped).
*Not chosen:* a `kind` attribute on each tool (simpler lookup, but it would put one client protocol's categories on every tool).

### D38. Tolerant of clients that stray from the spec
*Step 4 · design*

`session/cancel` is accepted as a notification (per the spec) or as a request (answered with null). A cancel also ends Wren's wait for a permission answer, without relying on the client to send "cancelled". Both came from reading Toad's client code.

### D39. Clients are shown the gist of a tool result, not all of it
*Step 4 · you*

Your choice of "compact, with key detail": a finished tool call sends the diff for an edit, the output for a command (the last 4,000 characters), the error for a failure, and a one-line summary otherwise. The model still receives the full result.
*Not chosen:* summaries only (edits under "always allow" would happen unseen); everything the model sees (noisy).
