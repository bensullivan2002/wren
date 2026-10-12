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

Steps 1 and 2 are done (see the roadmap in README.md). **Next: step 3, acting safely.** Planned order:

1. **Refactor tool inputs to Pydantic** (decision D13): one input model per tool generates the JSON Schema and validates arguments; delete `tools/validation.py`. Pydantic is already installed via the `anthropic` SDK. Journal it as a refactor: what changed and why it's better.
2. **An edit tool and a shell tool.**
3. **Permission prompts** in `ToolRegistry.run`, between validation and running.
4. **Rethink rollback on interrupt**: once tools have side effects, discarding the turn from history no longer undoes them.

Questions to put to Ben during step 3:

- Edit tool design: our own str_replace-style tool, or Anthropic's predefined text-editor and bash tool types, which Claude is trained on? Check the current docs before presenting this.
- Permission model: what needs asking, "always allow" scopes, and whether choices persist between sessions.
- What an interrupted turn should mean after side effects.

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/wren --provider fake      # offline demo: ls / read <path> / search <regex>
```
