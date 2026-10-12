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

Steps 1 to 6 are done (see the roadmap in README.md).

**First thing next session: ask Ben how real use is going.** He has confirmed `/usage`, `/compact` and `/history` work. Still worth asking about: tool loops against the real API (reasoning blocks, D45), whether `/usage` shows cache reads, and whether a `WREN.md` is actually followed. Fix what he finds before step 7.

**Then: step 7, sessions** (save the conversation to disk, list past sessions, resume one). Questions to put to Ben:

- Format and place: one JSON Lines file per session, appended as events happen, under `~/.wren/sessions/` (per project) or inside the project? Check how Claude Code and others store transcripts before presenting.
- What resuming restores: the history only, or also the "always allow" permissions (probably not: D23, D50)? What to say to the model about time having passed and files having changed.
- How to resume: `wren --resume` / `--continue` flags, a `/resume` picker in the app, or both.
- What to do with `ReasoningBlock`s on disk (they are provider-specific, D45) and with sessions saved by a different model or provider.
- Whether the app should redraw the old transcript on resume.

Things Ben has decided that aren't obvious from the code:

- **No ACP / Toad.** An ACP mode was built and removed in step 4 (D33): he found driving Wren through another program confusing, and wants no second way in. Don't re-propose it before step 8, and only if he raises it.
- **He tests with a paid API balance, not his Claude subscription.** Don't build subscription (OAuth) login.
- **He likes a full-screen, Claude Code-like feel:** input box at the bottom, conversation above, questions in place.

Carried over, for whenever they fit (see "Known limits" in journals 03 and 04):

- An async core (D34) if parallel tools or subagents need it; Escape can lag while waiting for the model's first words.
- Memory gaps: loading a subfolder's `WREN.md` on demand, imports, auto memory (D49), saved permission rules (D50).
- Context gaps: trimming old tool results before resorting to a summary (D40), compacting inside a very long turn (D47), keeping recent messages verbatim (D42 explains why not), showing cost in money.
- App gaps: expanding cut-off diffs and output, queueing a message while Wren works, showing a tool request as it's written (needs a provider event).

- Saving "always allow" rules between sessions (D23 left this out on purpose).
- The shell is not sandboxed, and "always allow `git`" covers every git subcommand (D28, D29).
- The tool-writing conventions are in `docs/ARCHITECTURE.md` ("Anatomy of a tool"): a nested Pydantic `Input(ToolInput)`, `permission()` for anything that changes things, and tests that use a fake asker.
- A front-end feature must work in both front ends or be clearly optional: `tui.py` and `repl.py` implement the same contract (ARCHITECTURE.md, "the front-end contract"). Nothing Textual-specific goes in `agent.py`, `tools/` or `commands.py`.
- The history is append-only between compactions, and requests are always built the same way (ARCHITECTURE invariants 2 and 16). Don't edit or reorder earlier messages, the system prompt or the tool list mid-session: it breaks prompt caching and can invalidate the model's reasoning blocks. `ReasoningBlock.data` is opaque: only its provider reads it.
- The system prompt is fixed for a conversation (invariant 17): memory is read at the start and on `/clear` only. Memory files are untrusted text (D53): never let one grant a permission or change what the registry enforces.
- In `tui.py`, only the app thread touches widgets, and text from the model, files or commands is never passed as markup. UI tests wait on conditions (`until`), never fixed sleeps.

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/wren --provider fake      # the app, offline: ls / read / search / write / edit / run
.venv/bin/wren --provider fake --plain   # the plain REPL
```
