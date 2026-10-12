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

Steps 1 to 4 are done (see the roadmap in README.md). Step 4 became "Wren speaks ACP, Toad is the UI" (D32), not a UI of our own.

**First thing next session: ask Ben how Wren behaved under real Toad.** ACP mode was built from the spec and a reading of Toad's client source, and tested only with our own test client (`tests/test_acp.py`). Launch: `toad acp "/path/to/.venv/bin/wren acp"`. Fix what he found before starting step 5.

**Then: step 5, context management** (token counting, a budget, compaction, prompt caching). It plugs in inside `Agent`, before each provider call. Questions to put to Ben:

- Compaction: our own summarising step, or Anthropic's server-side compaction and context editing (check the current docs, and what each does to D4/D5 provider neutrality)?
- When to compact: automatically at a threshold, on a `/compact` command, or both.
- Prompt caching: where the cache breakpoints go, and whether the date in the system prompt (which changes daily) should move.
- Whether to report context use to ACP clients (`usage_update`) and in the REPL.

Ben tests with a paid API balance, not his Claude subscription. Don't build subscription (OAuth) login: we discussed it and he chose the API route.

Carried over, for whenever they fit (see "Known limits" in journals 03 and 04):

- ACP gaps: saved sessions (`session/load`, with step 7), modes, plans, slash commands, images, `mcpServers` (step 8).
- An async core (D34) if parallel tools or subagents need it.
- The plain REPL got no markdown or spinner; a nicer built-in UI is unscheduled.

- Saving "always allow" rules between sessions (D23 left this out on purpose).
- The shell is not sandboxed, and "always allow `git`" covers every git subcommand (D28, D29).
- The tool-writing conventions are in `docs/ARCHITECTURE.md` ("Anatomy of a tool"): a nested Pydantic `Input(ToolInput)`, `permission()` for anything that changes things, and tests that use a fake asker.
- A new front-end feature must work in both front ends or be clearly optional: `repl.py` and `acp/server.py` are the same translation aimed at different targets. Nothing ACP-specific goes in `agent.py` or `tools/`.

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/wren --provider fake      # offline demo: ls / read / search / write / edit / run
.venv/bin/wren acp --provider fake  # ACP mode: JSON-RPC on stdin/stdout (for Toad)
```
