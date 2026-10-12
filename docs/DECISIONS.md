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
*Step 1 · design*

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
*Step 2 · you*

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
