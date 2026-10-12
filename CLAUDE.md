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

Steps 1 to 8 are done (see the roadmap in README.md). Step 8 was an unplanned addition, an OpenAI provider (D69); extensions moved to step 9.

**First thing next session: ask Ben how the OpenAI provider behaved with a real key.** It is tested only against a stand-in client built from the SDK's types (journal 08). Likely trouble spots, in order: replaying reasoning items between tool calls (`store: False`), an `/effort` level the model rejects, and the model-list filter. Fix what he finds first.

Confirmed by Ben against the real Anthropic API: `/usage`, `/compact`, `/history`, the model picker, and effort (a controlled low-versus-high comparison). Still unconfirmed there: tool loops with reasoning blocks (D45), cache reads showing in `/usage`, and resuming after a model switch.

**Then: step 9, extensions.** It is three separate pieces; the first question for Ben is which to build first, and each should be its own commit and journal entry:

- **MCP** (tools from other programs). Questions: stdio servers only, or HTTP too; hand-written client or the official `mcp` Python SDK (it is async: see D34); where servers are configured; and how permissions work for tools Wren did not write (every MCP tool call should ask, with "always" per tool). Check the current MCP spec first.
- **Subagents** (a task handed to a fresh conversation). Questions: which tools a subagent gets, whether it can ask the user for permission, how its result returns, whether it may use a different (cheaper) model or provider, and whether they run in parallel (D34 again).
- **Hooks** (the user's commands run at set moments). Questions: which moments, whether a hook can block an action, and where they are configured (a settings file, which also reopens saved permission rules, D50).

Things Ben has decided that aren't obvious from the code:

- **No ACP / Toad.** An ACP mode was built and removed in step 4 (D33): he found driving Wren through another program confusing, and wants no second way in. Don't re-propose it before step 8, and only if he raises it.
- **He tests with a paid API balance, not his Claude subscription.** Don't build subscription (OAuth) login.
- **He likes a full-screen, Claude Code-like feel:** input box at the bottom, conversation above, questions in place.
- **Wren is multi-provider (Claude and OpenAI), and he may want more.** Keep provider differences behind the `Provider` interface, and have front ends ask the provider what it offers; adding a provider should touch only `providers/`, `config.py` and `cli.py`. An "OpenAI-compatible" Chat Completions provider for other vendors was offered and deferred (D70).
- **He wants evidence, not assertion, in what Wren reports about itself.** He asked how to verify the model actually in use, then the same for effort. `/usage` labels each line with its source (reported by the provider, Wren's own record, or documentation with a date); keep to that, and never present a setting Wren sent as something the API confirmed (D64, D68).
- **He likes how Claude Code and Codex CLI present things, and says so.** Slash commands open in a panel above the prompt and close with Escape (D80) because he asked for that. When adding UI, match that feel: reference output in the panel, events as notes in the transcript, questions in the transcript.
- **He prefers one obvious way to do a thing over copying Claude Code's flags:** he rejected `--continue` plus `--resume` as confusing and chose `--resume` alone, opening the same picker as `/resume` (D57). Offer the simpler interface first.

Carried over, for whenever they fit (see "Known limits" in journals 03 and 04):

- An async core (D34) if parallel tools or subagents need it; Escape can lag while waiting for the model's first words.
- Provider gaps: no custom base URL, text only (journal 08).
- Session gaps: no expiry and no off switch (D63), no names or search, one folder at a time.
- Memory gaps: loading a subfolder's `WREN.md` on demand, imports, auto memory (D49), saved permission rules (D50).
- Context gaps: trimming old tool results before resorting to a summary (D40), compacting inside a very long turn (D47), keeping recent messages verbatim (D42 explains why not), showing cost in money.
- App gaps: expanding cut-off diffs and output, queueing a message while Wren works, showing a tool request as it's written (needs a provider event).

- Saving "always allow" rules between sessions (D23 left this out on purpose).
- The shell is not sandboxed, and "always allow `git`" covers every git subcommand (D28, D29).
- The tool-writing conventions are in `docs/ARCHITECTURE.md` ("Anatomy of a tool"): a nested Pydantic `Input(ToolInput)`, `permission()` for anything that changes things, and tests that use a fake asker.
- A front-end feature must work in both front ends or be clearly optional: `tui.py` and `repl.py` implement the same contract (ARCHITECTURE.md, "the front-end contract"). Nothing Textual-specific goes in `agent.py`, `tools/` or `commands.py`.
- The history is append-only between compactions, and requests are always built the same way (ARCHITECTURE invariants 2 and 16). Don't edit or reorder earlier messages, the system prompt or the tool list mid-session: it breaks prompt caching and can invalidate the model's reasoning blocks. `ReasoningBlock.data` is opaque: only its provider reads it.
- Every message enters the history through `Agent._add` (invariant 20); never append to `agent.messages` directly, or the session file misses it. The session file format is a promise (journal 07, Idea 3): add new line types, don't change existing ones, and bump `FORMAT_VERSION` only for a change old code can't read.
- On macOS there is no `timeout` command. If a test run hangs, it is almost certainly a UI test whose worker thread is waiting: run `tests/test_tui.py` alone with `-o faulthandler_timeout=20`.
- The system prompt is fixed for a conversation (invariant 17): memory is read at the start and on `/clear` only. Memory files are untrusted text (D53): never let one grant a permission or change what the registry enforces.
- In `tui.py`, only the app thread touches widgets, and text from the model, files or commands is never passed as markup. UI tests wait on conditions (`until`), never fixed sleeps.

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/wren --provider fake      # the app, offline: ls / read / search / write / edit / run
.venv/bin/wren --provider fake --plain   # the plain REPL
```
