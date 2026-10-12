# Working on Wren: notes for Claude

## What this project is

Wren is a coding-agent harness for the terminal, built step by step by Claude with Ben (GitHub: bensullivan2002) as a learning project. **Claude builds; Ben guides and decides.** There are two goals, both of which matter: Ben understands every part, *and* the end result is well-engineered.

## How we work

- **Ben decides the significant choices.** At each decision point, ask with a few options, a recommendation, the trade-offs, and what the industry standard is. Make smaller design calls yourself and log them.
- **Ben is often on his phone.** Keep messages short and scannable. He reads the code and docs on GitHub.
- **Every step ships with docs, updated as part of the step:**
  - `docs/journal/NN-name.md`: a teaching entry covering the ideas behind the step, the Python worth knowing, things to try, known limits, and what's next. Explain *why*, with real snippets from the code.
  - `docs/ARCHITECTURE.md`: rewritten to describe the code as it is now (parts, life of a turn, events, invariants, where next steps plug in).
  - `docs/DECISIONS.md`: append numbered decisions (D1, D2…), each marked **you** (Ben's call) or **design** (Claude's call, open to challenge), with what wasn't chosen and why.
  - `README.md`: status line, commands, tools, config, roadmap, file tree.
- **Code is written to be read.** Module docstrings explain why the module exists; comments teach the non-obvious. Prefer clarity to cleverness.
- **Tests:** `pytest`, using `FakeProvider`, so no network or API key is needed. Agent tests must leave `history_problems(agent.messages) == []`.
- **One commit per step** on `main`, unless Ben asks otherwise.

## Status

Steps 1 to 3 are done (see the roadmap in README.md). **Next: step 4, a better terminal UI.** It should replace `repl.py` (events in, `ask_permission` callback) without touching `agent.py` or `tools/`.

Questions to put to Ben during step 4:

- Which terminal UI approach: stay with plain printing plus a markdown renderer (e.g. Rich), or a full-screen TUI framework (e.g. Textual, prompt_toolkit)? Check what current coding agents use before presenting this.
- How much to show of tool calls and results (collapsed summaries versus full output), and how diffs and permission prompts should look.
- Whether to stream the model's tool requests as they're written (needs a new provider event).

Carried over from step 3, for whenever they fit (see journal 03, "Known limits"):

- Saving "always allow" rules between sessions (D23 left this out on purpose).
- The shell is not sandboxed, and "always allow `git`" covers every git subcommand (D28, D29).
- The tool-writing conventions are in `docs/ARCHITECTURE.md` ("Anatomy of a tool"): a nested Pydantic `Input(ToolInput)`, `permission()` for anything that changes things, and tests that use a fake asker.

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/wren --provider fake      # offline demo: ls / read <path> / search <regex>
```
