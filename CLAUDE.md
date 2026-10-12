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

Steps 1 to 5 are done (see the roadmap in README.md).

**First thing next session: ask Ben whether he has run Wren against the real API yet, and how it went.** Three things are tested only against stand-ins and need a real session to confirm (journal 05, "Known limits"): reasoning blocks surviving a tool loop (D45), prompt caching actually hitting (`/usage` should show cache reads from the second call on), and a real compaction's summary quality. Also ask how the app feels in his terminal. Fix what he finds before step 6.

**Then: step 6, project memory** (a `WREN.md` per project, loaded into every conversation). Questions to put to Ben:

- Where it's loaded from: the working directory only, or also parent folders and a personal file in the home directory (check how Claude Code's CLAUDE.md and the AGENTS.md convention do it, and whether to read those names too).
- Whether Wren may write to it (a `/remember` command, or the model editing it through the normal permissioned edit tool), or it's the user's file only.
- Where it goes in the request. It must not break caching (ARCHITECTURE invariant 16): the system prompt is stable per session, so loading it once at startup is safe; reloading mid-session is not free.
- Whether saved "always allow" permission rules (deferred in D23) belong in this step, in a separate settings file.

Things Ben has decided that aren't obvious from the code:

- **No ACP / Toad.** An ACP mode was built and removed in step 4 (D33): he found driving Wren through another program confusing, and wants no second way in. Don't re-propose it before step 8, and only if he raises it.
- **He tests with a paid API balance, not his Claude subscription.** Don't build subscription (OAuth) login.
- **He likes a full-screen, Claude Code-like feel:** input box at the bottom, conversation above, questions in place.

Carried over, for whenever they fit (see "Known limits" in journals 03 and 04):

- An async core (D34) if parallel tools or subagents need it; Escape can lag while waiting for the model's first words.
- Context gaps: trimming old tool results before resorting to a summary (D40), compacting inside a very long turn (D47), keeping recent messages verbatim (D42 explains why not), showing cost in money.
- App gaps: expanding cut-off diffs and output, queueing a message while Wren works, showing a tool request as it's written (needs a provider event).

- Saving "always allow" rules between sessions (D23 left this out on purpose).
- The shell is not sandboxed, and "always allow `git`" covers every git subcommand (D28, D29).
- The tool-writing conventions are in `docs/ARCHITECTURE.md` ("Anatomy of a tool"): a nested Pydantic `Input(ToolInput)`, `permission()` for anything that changes things, and tests that use a fake asker.
- A front-end feature must work in both front ends or be clearly optional: `tui.py` and `repl.py` implement the same contract (ARCHITECTURE.md, "the front-end contract"). Nothing Textual-specific goes in `agent.py`, `tools/` or `commands.py`.
- The history is append-only between compactions, and requests are always built the same way (ARCHITECTURE invariants 2 and 16). Don't edit or reorder earlier messages, the system prompt or the tool list mid-session: it breaks prompt caching and can invalidate the model's reasoning blocks. `ReasoningBlock.data` is opaque: only its provider reads it.
- In `tui.py`, only the app thread touches widgets, and text from the model, files or commands is never passed as markup. UI tests wait on conditions (`until`), never fixed sleeps.

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/wren --provider fake      # the app, offline: ls / read / search / write / edit / run
.venv/bin/wren --provider fake --plain   # the plain REPL
```
