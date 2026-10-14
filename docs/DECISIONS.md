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
*Step 3 · design* · **Sandboxing superseded on macOS by D101 in step 9.**

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

### D32. Wren's interface is its own full-screen Textual app
*Step 4 · you*

`wren` opens a full-screen terminal app built on Textual: a scrolling conversation of rendered markdown, a prompt box, tool calls with diffs, and permission questions in place.
*The route:* the first recommendation was an inline UI on Rich and prompt_toolkit. We then built an ACP mode so that Toad, a third-party Textual app, could be the interface (see D33). You chose Wren's own Textual app after trying that.
*Not chosen:* Rich + prompt_toolkit (inline, keeps the terminal's scrollback, the least machinery; Aider's approach). Rich alone. A third-party client over ACP (D33).
*Cost:* an event-driven, two-thread program is harder to follow than a REPL; the conversation isn't in your terminal's scrollback after you quit; Textual is a sizeable dependency.

### D33. An ACP mode was built, then removed
*Step 4 · you*

For one commit (`f2f8e5e`), `wren acp` made Wren an Agent Client Protocol agent: a hand-written JSON-RPC server on stdin and stdout, so that Toad or an editor could drive it. It was removed in the same step.
*Why removed:* driving Wren through another program felt like leaving Wren, and a second way in would confuse users and add code to keep working.
*What stayed:* the two neutral hooks it prompted (D36), which the Textual app needed too.
*If it comes back:* the natural place is step 8, beside MCP. The front-end contract in ARCHITECTURE.md is what it would implement.

### D34. Threads, not async: a turn runs on a worker thread
*Step 4 · design*

The agent, providers and tools stay synchronous. The app runs each turn on a worker thread, which reaches the screen only through `call_from_thread`. Stopping is cooperative: a flag the worker checks at each event, and the shell polls.
*Not chosen:* rewriting the core as async (how most production agents are built; stopping would be immediate everywhere, but every core file and most tests would change).
*Cost:* Escape can lag while the model is producing its first words. This question was put to you and left unanswered when plans changed, so it's a design call, and worth revisiting if parallel tool calls or subagents (step 8) need it.

### D35. The plain REPL stays, as `--plain` and for pipes
*Step 4 · design*

`repl.py` remains. Wren uses it when asked (`--plain`) or when input or output isn't a terminal. It needs nothing but `print` and `input`, it's what the end-to-end tests drive, and a second front end keeps the boundary between agent and interface honest.
*Not chosen:* deleting it (less code, but no fallback, no scripting, and subprocess tests would need a terminal emulator).

### D36. Two neutral hooks for front ends; nothing Textual-specific in the core
*Step 4 · design*

The tools layer gained `FileChange` (an edit as before-and-after text, on `PermissionRequest` and `ToolResult`) and `ToolContext.should_stop` (so a long-running tool can be stopped). Slash commands moved to `commands.py`, returning text. Textual is imported only by `tui.py`.

### D37. Tool calls are shown compactly, with the detail that matters
*Step 4 · you*

One line per tool call. Edits always show their diff; commands show the last lines of their output; everything else shows a one-line summary. The model still receives the full result.
*Not chosen:* summaries only (edits under "always allow" would happen unseen); everything the model sees (noisy; could be a later verbose mode).

### D38. Permission questions appear in the conversation, not in a dialog
*Step 4 · design*

A question is a widget in the transcript that takes the keyboard focus and is answered with one key. It reads in order with what led to it, and you can still scroll back while deciding.
*Not chosen:* a modal dialog over the screen (more prominent, but it hides the context you need to decide).

### D39. Enter sends; Ctrl+J adds a line; Escape stops; Ctrl+Q quits
*Step 4 · design*

Ctrl+C is left as copy, Textual's convention, so text can be copied out of a full-screen app. The REPL's Ctrl-C-to-stop therefore becomes Escape in the app.
*Not chosen:* Shift+Enter for a new line (many terminals can't tell it from Enter). Ctrl+C to stop (it would take away copying).

---

### D40. Compaction is our own summary step
*Step 5 · you*

When the conversation is too large, Wren asks the model to summarise it (`COMPACT_PROMPT`) and replaces the history with that summary.
*Why:* it works with any provider (D4, D5), the prompt and the result are readable and editable, and it's what Claude Code and most agents do.
*Not chosen:* Anthropic's server-side compaction (least code, but a beta, Claude-only, and the summary is opaque). Dropping only old tool results (cheap and loses no dialogue, but the conversation still grows without limit; it would make a good gentler first step later).
*Cost:* one extra model call per compaction, and a summary always loses detail.

### D41. Compaction runs automatically at a budget, and on `/compact`
*Step 5 · you*

Wren compacts between turns once the conversation passes a budget: 150,000 tokens by default (the same default Anthropic's own feature uses), set with `--context-budget`, and 0 turns it off. `/compact` does it on request. A meter shows how close the budget is.
*Not chosen:* manual only (predictable, but easy to forget). Automatic only (no way to compact early at a natural break, which is when a summary loses least).
*Since D99:* there's no fixed default. The budget is just short of the model's own window, and `--context-budget` is a cap that can only lower it.

### D42. Compaction replaces everything; no recent messages are kept verbatim
*Step 5 · design*

The new history is a note and the summary, nothing else.
*Why:* a model's reasoning blocks are tied to the conversation that preceded them. Keeping recent messages while replacing what came before would leave their reasoning attached to a history that no longer exists, which the API can reject. A clean break avoids it, and is simpler.
*Not chosen:* keeping the last few messages word for word (kinder to continuity; revisit if a provider-neutral way to do it safely appears).

### D43. The conversation's size is read from each reply, not counted
*Step 5 · design*

`context_tokens` comes from the usage the API reports with every reply: exact, free, and one call behind.
*Not chosen:* estimating from characters (wrong, badly so for code). Calling the token-counting endpoint before each request (accurate, but an extra round trip for a number we're about to be given).

### D44. Prompt caching is on for every request, placed automatically
*Step 5 · design*

The Anthropic provider adds a top-level `cache_control`, which caches up to the end of each request and moves forward as the conversation grows.
*Not chosen:* placing cache breakpoints by hand (finer control, more to get wrong; no need yet). Moving the date out of the system prompt (it changes the prefix once a day, and the cache lasts five minutes, so it costs nothing). The one-hour cache (double the write price; worth it only if pauses between messages are usually long).

### D45. Reasoning blocks are stored sealed and sent back unchanged
*Step 5 · design · fixes a bug*

Since step 1 the Anthropic provider had dropped any reply block it didn't recognise, including the model's thinking. Current models think by default and expect those blocks back. They're now kept as `ReasoningBlock`s: provider-specific data, returned in place, opened only by the provider that made them.
*Cost:* one vendor-shaped thing in an otherwise neutral history (D5). It is opaque by design, so nothing else depends on its shape.

### D46. The default `max_tokens` is 32,000
*Step 5 · design*

Up from 8,192. The model's thinking counts against this limit, so the old value could cut a reply off before it reached its answer. It's a ceiling, not a cost.

### D47. Compaction happens between turns, and fails safe
*Step 5 · design*

Automatic compaction is checked at the start of a turn, when the history is whole. Every step that can fail comes before the single line that replaces the history, so a failure or an interruption leaves it untouched; a failed automatic compaction lets the turn go ahead.
*Cost:* a single very long turn can overshoot the budget. The budget is a cost target, well below the model's real limit. (Since D99 the budget sits near the window, and the 10% margin is there for such a turn.)

---

### D48. Memory is `WREN.md`, else `AGENTS.md`, plus a personal file
*Step 6 · you*

Wren loads `~/.wren/WREN.md`, then for each folder from the project's root down to the working directory, `WREN.md` or, where there is none, `AGENTS.md`. All are joined, most general first.
*Why:* it mirrors Claude Code's scheme, gives you a place for preferences that apply everywhere, and makes repositories already set up for other agents work with no extra file.
*Not chosen:* `WREN.md` in the working directory only (simplest, but nothing personal and nothing from a monorepo's root). `AGENTS.md` only (one file for every agent, but nowhere for Wren-specific or personal notes).

### D49. Memory is the user's file; `/remember` appends; the model edits it only with permission
*Step 6 · you*

`/remember <note>` adds a line to the project's memory file. The model can change the file through the ordinary edit tools, which ask first and show a diff.
*Not chosen:* read-only (most predictable, but capturing a lesson means leaving Wren). Auto memory, where Wren keeps notes unprompted (useful; a bigger step in trust, and more to build and prune).

### D50. Saved permission rules are not part of this step
*Step 6 · you*

"Always allow" stays per-session (D23).
*Why:* it needs a settings file and rule matching of its own, and a project file that grants permissions is what a hostile repository would ship. Better as a separate, deliberate step.

### D51. The search for project files stops at the top of the git repository
*Step 6 · design*

Wren looks in the working directory and the folders above it as far as the one containing `.git`; outside a repository, only the working directory.
*Not chosen:* walking up to the filesystem root, as Claude Code does (it would load a stray `WREN.md` or `AGENTS.md` from a shared parent such as `/tmp`). Subfolders below the working directory aren't loaded either; doing that on demand is a later refinement.

### D52. Memory goes in the system prompt, read once per conversation
*Step 6 · design*

The files are rendered into the system prompt when a conversation starts and when it's cleared. Edits to a file, including `/remember`, apply from the next conversation.
*Why:* the system prompt is at the front of every request, and the prompt cache matches from the front (D44). Re-reading memory per request would make every request after a change a cache miss; the system prompt also survives compaction, so memory does too.
*Not chosen:* injecting memory as a message (it would be summarised away by compaction). Reloading on every turn.

### D53. Memory files are treated as instructions from outside
*Step 6 · design*

A cloned repository's `AGENTS.md` enters the prompt unread by the user. So: loaded files are announced at startup and listed by `/memory`; each is labelled in the prompt with its path and scope; the header tells the model they are guidance and cannot approve anything; each file is capped at 20,000 characters. None of that is enforcement. The enforcement is the permission check (D23, D26), which a file can't influence.

### D54. `/remember` writes to the project file in use, even an `AGENTS.md`
*Step 6 · design*

The note goes to the project memory file nearest the working directory, and a new `WREN.md` is created only if there is none at all.
*Why:* a new `WREN.md` beside an existing `AGENTS.md` would shadow it (D48), and the project's instructions would silently stop loading.
*Cost:* `/remember` may add a line to a file shared with other tools and people. It says which file it wrote.

---

### D55. Model switching is built now, with a picker
*Step 7 · you*

`/model` opens a list of the provider's models to choose from; `/model NAME` switches directly. It wasn't on the roadmap; you asked for it, and it belongs with sessions because a saved session should record its model.
*Not chosen:* `/model NAME` only (no picker; you'd need to know the names). Leaving it for later.

### D56. Sessions live in the home folder, per project, as JSON Lines
*Step 7 · you*

`~/.wren/sessions/<project>/<id>.jsonl`, one file per conversation, appended to as it happens. This is Claude Code's layout.
*Why:* transcripts hold file contents and command output, which can include secrets. Outside the repository they can't be committed by accident.
*Not chosen:* a `.wren/sessions/` folder inside the project (easy to find, travels with the folder, and one `git add .` from being published). One flat folder for every project (simplest, but "the sessions for this folder" means opening every file).

### D57. One word for going back: `--resume`, and `/resume`
*Step 7 · you*

`wren --resume` opens Wren on the list of this folder's saved conversations, the same list `/resume` shows inside it. `wren --resume ID` goes straight to one.
*The route:* the first plan copied Claude Code: `--continue` for the most recent, `--resume` for a picker. You found two words for nearly one thing confusing, then asked for `--resume` alone to open the picker.
*Cost:* getting back to the most recent conversation takes one more key press (Enter on the top row) than a dedicated flag would.

### D58. The session file is a log; the history is rebuilt by replaying it
*Step 7 · design*

Each event is one appended line: `meta`, `message`, `replaced` (a compaction), `model`. Nothing written is ever changed.
*Why:* nothing to remember to save, a crash costs at most the line in progress, and writing doesn't slow as the conversation grows.
*Not chosen:* rewriting a snapshot of the whole history on each change (simpler to read back; destroyed if interrupted mid-write). A database such as SQLite (robust and queryable, but opaque: you couldn't `cat` a session).
*Cost:* a compacted conversation's old messages stay in the file. That allows recovery from a bad summary, and means compaction doesn't remove anything from disk.

### D59. The agent reports to a small journal, through one method of its own
*Step 7 · design*

`Agent.journal` is anything with `append`, `replace`, `model_changed`, `spent` and `reset`. All additions to the history go through `Agent._add`.
*Not chosen:* the front end saving the history after each turn (less in the agent, but nothing is saved mid-turn, which is when a crash is likeliest). The agent writing files itself (it would then know about paths and formats).

### D60. Resuming restores the conversation, its model and its token counts; not permissions
*Step 7 · design · revised*

"Always allow" permissions are not restored: they were granted for one sitting (D23, D50). The system prompt is rebuilt from the memory files as they are now. The model is restored when the session's provider is the one in use.
*Revised after you reported it:* token totals and the conversation's size were at first not saved, so a resumed conversation showed 0. Each model call now writes a `spent` line, and resuming adds them up. Totals are per conversation: `/clear` resets them.

### D61. A session cut off mid-turn is tied off on resume, or refused
*Step 7 · design*

`Agent.restore` answers pending tool requests with "no result was recorded… it may or may not have run", adds a note, and then requires `history_problems` to be empty. A history that still isn't valid is not loaded, and the current conversation is kept.
*Not chosen:* dropping the unfinished turn (it would hide a command that may have run). Loading whatever is there (the API would reject every request).

### D62. The model list is fetched from the provider, not written in the code
*Step 7 · design*

`Provider.models()` asks the API when the picker opens.
*Not chosen:* a hard-coded list (works offline, and wrong within months).
*Cost:* a network call to open the picker; if it fails, you can still switch by name.

### D63. Sessions are saved with no expiry and no off switch, for now
*Step 7 · design*

Every conversation is kept indefinitely. Both an age limit and a way to turn saving off are obvious next additions; neither was needed to make resuming work. Open to challenge: for a tool that records what it reads, an off switch may deserve to come sooner.

### D64. `/usage` shows the model the API says answered
*Step 7 · design*

Each reply records the model named in the provider's response, and `/usage` shows it beside the model Wren asked for. Added when you asked how to tell whether the picker really changes anything: the header only shows Wren's intention.
*Not chosen:* asking the model what it is (models often don't know, or get it wrong).

### D65. Effort is a provider capability that may be separate, or part of the model
*Step 7 · you*

You asked for an effort setting, and for it to cope with providers that bundle effort into the model choice. A provider has `effort` and `efforts()`. Where effort is a separate request setting (Anthropic), `efforts()` lists the levels and `/effort` picks one. Where it's part of the model's name, `efforts()` is empty, the variants are in `models()`, and `/effort` points to `/model`.
*Not chosen:* one combined model-and-effort picker for every provider (it would multiply Claude's model list by five, and hide that they are the same model). Effort handled only inside the Anthropic provider (the next provider would have to redo the interface).

### D66. Effort levels are read from the API per model; unset is not sent
*Step 7 · design*

`AnthropicProvider.efforts` reads the model's reported capabilities. With no effort set, the request omits the field and the model's own default applies.
*Not chosen:* a fixed list in the code (levels differ by model, and change). Defaulting to a level of our choosing (the right default depends on the model; the provider already picks one).

### D67. Switching model resets effort to the new model's default
*Step 7 · design*

*Why:* the levels differ between models, and an unsupported one fails every request. Checking would need a network call during the switch.
*Cost:* you set effort again after changing model. Wren tells you when it has dropped one. Open to challenge: keeping the level when the new model reports it as supported would be kinder.

### D68. `/usage` reports effort with its sources, and doesn't claim confirmation
*Step 7 · design*

You asked what a model's default effort is, and for evidence of the effort used, as there is for the model. The API reports neither. So `/usage` shows: the default for the model from a table of documented values, with its date and "the API doesn't report it" (unknown models are shown as unknown); the effort the last request was sent with, labelled as Wren's own record; and the thinking tokens the provider reported for the last reply, the one measurable effect of effort.
*Not chosen:* showing "effort: high" as if confirmed. Leaving the default unexplained (what you reported). Guessing a default for models not in the table.
*Cost:* a hard-coded table, against the grain of D62 and D66. It exists only because the API has no way to ask.

---

### D69. A second provider, OpenAI, before extensions
*Step 8 · you*

You asked for an OpenAI provider ahead of MCP, subagents and hooks, which move to step 9. It tests D4 and D5 (Claude first, swappable later; Wren's own message types) with a real second model.

### D70. The OpenAI provider uses the Responses API
*Step 8 · you*

OpenAI's current API and its recommendation for reasoning models: reasoning carries across tool calls, and usage reports reasoning and cached tokens.
*Not chosen:* Chat Completions (the older API, and the one most "OpenAI-compatible" services copy; reasoning isn't carried between tool calls). Both, as two providers (most reach; twice the translation to maintain, and better as its own step).

### D71. It uses the official `openai` SDK
*Step 8 · you*

Consistent with the Claude provider (D3). The package is imported only when the provider is chosen.
*Not chosen:* hand-written HTTP (no dependency, every byte visible; but we would own retries, streaming and error handling).

### D72. Effort is a separate `/effort` for OpenAI, as for Claude
*Step 8 · you*

In OpenAI's API, effort is a request setting (`reasoning.effort`), not part of the model's name; the bundling is how ChatGPT and Codex present their pickers. So `/model` and `/effort` work the same way on both providers.
*Not chosen:* model-and-effort pairs in the model list. Both at once (two ways to do one thing).
*Note:* the interface still allows a provider to fold effort into its models (D65); only the fake provider does today.

### D73. Wren keeps the history for OpenAI too: `store: False`
*Step 8 · design*

Every request carries the whole conversation, with the model's reasoning as encrypted items that are replayed unchanged.
*Not chosen:* OpenAI's server-side conversation state (less to send, but compaction, sessions, `/history` and switching provider all assume the history is `agent.messages`).

### D74. Tools are declared `strict: False`
*Step 8 · design*

Left unset, the API tries to convert a schema to strict mode, where optional arguments become required-but-nullable. Wren's tools have optional arguments with defaults and reject null (D25), so a converted schema would make the model send arguments Wren refuses.
*Cost:* OpenAI doesn't guarantee arguments match the schema. Wren validates them itself, as it does for every provider.

### D75. For OpenAI, `/effort` offers every level, and defaults come from documentation
*Step 8 · design*

OpenAI's API can't be asked which levels a model accepts, so the provider offers all of them and an unsupported one is refused on the next request. Default effort comes from a dated table copied from OpenAI's guide; uncovered models show as unknown (as D68).
*Not chosen:* a table of supported levels per model (it would be wrong within months, and wrong silently).

### D76. Each provider has its own default model; OpenAI's is `gpt-5.6-terra`
*Step 8 · design*

The mid-priced option, by the reasoning of D11 for Sonnet. `/model` or `--model` changes it.

### D77. OpenAI's model list is filtered by name
*Step 8 · design*

The API lists every model on the account, including image, speech and embedding models. The picker keeps ids starting `gpt-` or `o` and drops those containing words such as `embedding` or `audio`. It is a heuristic; `/model NAME` accepts any name.

### D78. `/usage` exposes OpenAI's response effort separately from the request
*Step 8 follow-up · design*

You asked to check effort from the OpenAI API response, not local settings. The installed SDK exposes optional `response.reasoning.effort`; the earlier blanket claim that no API returns effort was wrong for OpenAI. `Completed.effort_reported` carries that field to `/usage`, alongside the existing `effort_sent` request record and thinking-token counts. Absent or null response effort stays unknown; the string `"none"` is a valid reported level. This extends D68 without treating request settings or token counts as API-confirmed effort.
*Not chosen:* falling back to the requested effort, inferring effort from thinking tokens, or calling response metadata a measurement of internal work. Saving last-call metadata in sessions is left out of this change; restart, send a message and use `/usage` for a fresh response check. Tests use SDK-validated stand-ins; this is not a claim of live API verification.

### D79. `/provider` switches provider mid-conversation
*Step 8 · you*

You asked to change provider without restarting. `/provider` opens a picker (or `/provider NAME`); the conversation carries over, and the session records the change.
*Design details:* a provider without its API key is shown as such and can't be chosen. Switching back to a provider returns to the model last used with it in this conversation. Resuming a session returns to its provider if the key is available.
*Cost:* one uncached request, and the previous model's private reasoning doesn't cross providers.

### D80. Slash commands open in a panel that Escape closes
*Step 8 · you*

You asked for commands to open the way they do in Claude Code and Codex: inline, and dismissed with Escape. Reference output (`/usage`, `/help`, `/history`, `/tools`, `/system`, `/memory`, `/permissions`) and the pickers now open in a panel above the prompt, and nothing is added to the transcript.
*Design details:* commands that change something (`/clear`, `/remember`, `/model NAME`, `/compact`) and errors still leave a note in the transcript, as a record. With the panel open, Escape closes it and does not also stop a running turn. A permission question closes the panel. This revises D38 for pickers, which used to sit in the transcript; permission questions stay there.
*Not chosen:* a full-screen modal (it hides the conversation you may be asking about).

### D81. New TUI conversations show the terminal Wren logo
*Branding follow-up · you · placement superseded by D82*

You expected the logo to appear after restarting Wren. The original logo PR shipped assets and README branding only; the follow-up puts the bird in the scrolling transcript at startup and after `/clear`.
*Design details:* the welcome is a widget, not a model message or saved session record. Existing histories resume without a new banner. The mark is bundled as package data and kept in sync by the branding generator, so installation and working directory do not matter. It inherits the theme's accent and falls back to `wren` below 20 columns or 20 rows, updating on resize.
*Not chosen:* a permanent eight-row header (less room for the conversation), a repo-relative asset path (breaks installed apps), or a logo in plain/piped output (decoration in scripts).

### D82. Pin the TUI logo at the top left
*Branding follow-up · you · small-terminal threshold raised by D96*

You asked for the logo to be pinned like Claude and Codex, rather than scrolling away. This supersedes D81's placement: a top-docked screen widget appears above the header in every conversation, including resumed ones. `/clear` and history redraws touch only the transcript, leaving the same logo widget in place.
*Design details:* reserve the logo's space outside a flexible body, and reserve the status and prompt outside the body's flexible chat area. The command panel's percentage height is relative to that chat area, so opening `/help` with a multiline input does not push the prompt off-screen. Theme colour, packaged asset, small-terminal fallback, and absence from model history and plain output are unchanged.
*Cost:* the full bird reserves eight rows, plus the existing one-line header. Below 20 columns or 20 rows it becomes the single-line `wren` fallback.

### D83. Shell commands must come with a plain-English description
*Step 8 · you*

You pointed out that being asked to approve a long chained command is meaningless. The shell tool now requires a `description`: one sentence, written by the model, saying what the command does and why. The permission prompt leads with it, labelled "Wren says", and always shows the command itself underneath.
*Why labelled:* it is the model's claim about its own action, and a model can be wrong or misled. The command is what runs.
*Not chosen:* an optional description (models skip optional fields). Having Wren explain the command itself by parsing it (shell is too varied to parse reliably). A second model call to explain each command (slower, costs more, and is still a model's claim). Descriptions on edits too (a diff already shows exactly what changes).
*Cost:* a few more output tokens per command, and a description is only as honest as the model that wrote it.

### D84. The transcript speaks in plain English and marks who is speaking
*Step 8 · you*

You asked for clearer separation between blocks, above all between your input and everything else. Your messages, the model's and the harness's now each have a distinct bar down the left edge, and yours sit on a tinted band. Tool calls are worded as actions ("Read src/app.py"), with a shell command's description as the headline and the command beneath; `/history` shows the calls as made.
*Not chosen:* boxes around every message (heavy, and they waste width). Labels such as "You:" and "Wren:" on each block (noisier than colour, though more accessible: worth revisiting if the colours don't read well in your terminal).

### D85. Defaults: Opus 5.5 and GPT 6.1 Sol at `xhigh`, and a saved default that outranks them
*Post-step 8 · you*

You wanted the strongest model at extra-high effort to be the starting point: `claude-opus-5-5` for Anthropic, `gpt-6.1-sol` for OpenAI, both at `xhigh`. This replaces the mid-priced defaults of D11 and D76, and puts an explicit effort where D66 deliberately sent none. The order for model and effort is: flag, environment variable, your saved default, Wren's default, and (for effort) the model's own.
*Not chosen:* a system-wide config file between the saved default and Wren's (nothing needs one yet, and the built-in table plays that part).
*Cost:* every new conversation now runs at the most expensive setting unless you say otherwise. If a model refuses `xhigh`, the API's error shows on the first request, as it already does for OpenAI efforts (D75).

### D86. Enter in a picker keeps a choice for good; Space keeps it for the session
*Post-step 8 · you*

In the `/model` and `/effort` pickers, Enter makes the choice your default for every session, written to `~/.wren/settings.json` (per provider, the model and its effort together). Space chooses for this session only and leaves the saved default alone. The picker's footer says so. Typed `/model NAME` and `/effort LEVEL`, and the plain REPL (which has no picker), never save.
- *Starting Wren is not choosing a model.* A new model resets effort to its own default (D67), but a saved pair is applied as it was saved, effort included, and effort is only applied to the model it was saved with (`--model other` starts `other` at its own effort).
- *Choosing a model with Enter saves it with its own default effort* (null in the file, distinct from "nothing saved"); choosing an effort with Enter afterwards saves that level.
- *An effort chosen with Enter on a model chosen with Space is not saved*, with a note saying why: the level belongs to the model, and saving the model would break the Space promise. Applying it to the session still works.
- Switching provider starts on that provider's default pair when it lands on the default model; resuming a session brings its own model and effort.
*Not chosen:* saving on typed commands (they are explicit, one-off choices, and an easy way to change a default by accident). Ctrl+Enter or Shift+Enter as the second key (most terminals can't tell them from Enter). A per-model table of saved efforts (more to explain, and a stale level would silently return after a later model choice).

### D87. A footer replaces the header and the status line
*Step 8 follow-up · you*

You asked for a footer like [claude-hud](https://github.com/jarrodwatts/claude-hud)'s: fixed rows at the bottom of the app saying what Wren is set to, what it has spent, and what it's doing. The header line is gone and its contents moved into the footer; the pinned logo (D82) stays at the top. Six rows: settings, context, spending, time, tools, and what Wren is doing now.
*Your choices:* model and effort as plain `key: value`, with no note of where each value came from. Included: git branch; cost and £/min; tokens in, out, cache read and cache write; tokens per turn; turns; session and turn time; calls per tool. Also every extra proposed: cache hit, a warning when another model answered, step n/25, the "always allowed" count, thinking tokens, compactions, session id and whether it was resumed, memory files, files changed, tool errors, time to first word.
*Front ends:* the app only. The plain REPL keeps its one-line summary after each turn (`↳ …`): a line-by-line interface has no bottom of the screen to keep.

### D88. The context bar measures against the compaction budget
*Step 8 follow-up · you*

The bar fills towards `context_budget` (150,000 by default), where Wren compacts, not towards the model's real context window. It answers "how long until compaction?", which is the limit you meet first.
*Not chosen:* the model's window (a per-model table to maintain, and the bar would never get past the budget anyway).
*Since D99:* the bar still measures against the budget, but the budget is now worked out from the model's window.

### D89. Cost in pounds, from a dated price table per provider
*Step 8 follow-up · you*

Each provider has a `DOCUMENTED_PRICES` table, in US dollars per million tokens, and offers it through `Provider.prices()`. The footer converts at the day's exchange rate (D97). Burn rate is £ per minute. A model missing from its table shows `£?`; if only some calls were to such models, the total gets `+?`.
*Your choice (prices):* copied from the providers' published pages, each table naming its page and the date checked (`PRICES_SOURCE`): Anthropic's 11 current model IDs, and 9 of OpenAI's. A script compared every entry with the downloaded pages: no differences.
*Design details:* money is `Decimal`, not float. Thinking tokens are priced as output, since they are output. Cache writes use the 5-minute rate, the cache Wren asks for. Standard tier only; Wren uses none of the options that change prices (fast mode, US-only or regional routing). £/min shows `—` in the first minute, when a single reply would make it look enormous.
*Long prompts:* some models charge more for every token of a call whose prompt is long: Claude Haiku 5.5 over 100,000 tokens, which Wren's default budget passes, and several OpenAI models over 272K. `Prices` takes an optional second price and its threshold. Where a page says long prompts cost more but not how much (gpt-5.5, gpt-5.4), such a call counts as unpriced.
*Cost:* the tables go stale when prices change, and a new model shows `£?` until it's added.

### D90. Session time counts from when Wren opened
*Step 8 follow-up · you*

*Extended (proposed as design, then agreed by you):* so does everything else in the footer, so that a rate divides money and minutes that start at the same moment. After `/resume` the footer's tokens and cost leave out the resumed conversation's earlier totals (`/usage` still shows those). `/clear` resets nothing: the money was spent.
*Not chosen:* carrying the old totals over. Session files don't record how long a conversation took, so £/min would be wrong.

### D91. The footer sits below the prompt
*Step 8 follow-up · design*

Claude Code's status line, which claude-hud fills, sits below its input box (from memory: the claude-hud page couldn't be opened while building this). The box you type in stays next to the conversation.
*Not chosen:* above the prompt, where the old status line was (six rows between the conversation and your reply to it).

### D92. A footer row never wraps; parts are kept by importance
*Step 8 follow-up · design*

Each part of a row has a `keep` score from 1 to 9. In a narrow terminal, `footer.fit` takes the highest first and leaves out what doesn't fit, so every row stays on its line, and the model, the context bar and the cost are the last to go.
*Not chosen:* wrapping (rows would move about, and the footer's height would change). Cutting each row off at the right edge (it loses whatever happens to be on the right).

### D93. How the footer's figures are counted
*Step 8 follow-up · design*

- **tokens/turn:** the last finished turn, with the average of all finished turns beside it.
- **cache hit:** the share of everything sent since Wren opened that was read from the cache.
- **tool errors:** a "no" from you isn't counted. The tool didn't fail; you declined.
- **files changed:** distinct files that a tool changed successfully.
- **"last reply came from X":** shown when the model the provider reports doesn't start with the one asked for. Providers answer with dated names (asked `x`, answered `x-2026-09-01`), so this is a heuristic.
- **step n/25:** model calls in this turn, the number the step limit counts. With no limit (the default since D98) it's just `step n`.
- **first word:** from sending a request to its first text, for the latest model call that wrote any.

The footer is redrawn ten times a second while Wren works, and once a second otherwise, not on every event: a fast reply sends dozens of fragments a second.

### D94. The git branch comes from `git status`, on a worker thread
*Step 8 follow-up · design*

`git status --porcelain=v2 --branch` gives the branch and the number of uncommitted files, in a format git promises not to change. It runs at startup, after each turn, and every 10 seconds between turns, on its own worker thread so a slow repository can't freeze the screen. Two settings make it safe to run unasked: `GIT_OPTIONAL_LOCKS=0`, so it never holds the index lock while you run git yourself, and `core.fsmonitor=false`, so a downloaded repository's config can't make it run a program.
*Not chosen:* reading `.git/HEAD` directly (fast, but no uncommitted count, and worktrees and packed refs complicate it). A git library (a dependency for one command).

### D95. `Compacted` reports what the summarising call cost
*Step 8 follow-up · design*

Compaction calls the model but yields no `Completed`, so a front end had no way to see its tokens. `Compacted.usage` carries them, even when compaction fails: the call was still paid for.

### D96. The full logo needs 30 rows
*Step 8 follow-up · you*

The logo and the footer are both docked, so their rows are reserved first. On an 80×24 terminal, logo (8) + footer (6) + a prompt at its 10-line cap is all 24 rows, and the prompt was clipped by a row (the test caught it: prompt bottom 19, footer top 18). Below 30 rows the logo is now the one-line `wren`, which leaves the conversation at least 6 rows with a full prompt. The 20-column rule from D82 is unchanged.
*Not chosen:* a two-row footer on short screens (the footer would change shape, and lose what it's for). A shorter prompt cap (less room to write in every terminal, to save rows in small ones).

### D97. The exchange rate is looked up once a day
*Step 8 follow-up · you (a daily live rate) · design (the source and the details)*

The first time the app opens each day, a worker thread asks [Frankfurter](https://frankfurter.dev) for the European Central Bank's dollar-to-pound reference rate and saves it in `~/.wren/exchange-rate.json`. Every run that day uses the saved rate; an app left open past midnight looks again. This is the one network call Wren makes that isn't to the model. It's a fixed URL and sends nothing about you or your work.
*Why this source:* free, no key or account, and the rate comes from a central bank, not a trading feed. It is published on working days only, so at the weekend the rate is Friday's; the file records both the rate's date and the day it was fetched.
*Failure:* at most one try a day. If it fails, the last saved rate is used, however old. With no rate ever, costs are shown in dollars (`$0.42`) rather than converted at a made-up rate.
*Design details:* `exchange.py` takes the fetch and today's date as arguments, so tests never touch the network. The app does no lookup unless given an `ExchangeRate`, which only `cli.py` does. The file is written to a temporary file and renamed into place, so two Wrens starting at once can't leave it half-written. A rate outside 0–10 is not believed.
*Not chosen:* a fixed rate in `config.py` (goes stale silently). A lookup every run (needless calls when the bank's rate changes once a day). A paid or keyed service (one more secret to manage).

### D98. No step limit by default
*Step 8 follow-up · you · replaces D16's default*

A turn now runs until the model stops asking for tools, or you press Escape. `--max-steps N` (or `WREN_MAX_STEPS`) still sets a limit, which ends the turn with D16's note; 0 means no limit, as 0 means "never" for `--context-budget`. The footer shows `step n`, or `step n/N` when a limit is set.
*Why:* your call. Escape already stops a runaway turn at any point, and Claude Code doesn't cap an interactive turn by default either (its `--max-turns` is opt-in, for scripted runs).
*Cost:* a confused model can loop, spending money, until you notice and stop it.

### D99. Compact just short of the model's context window
*Step 8 follow-up · you (the formula, the unknown case, the cap, the sources) · design (the lookup and the wording) · replaces D41's default*

Wren compacts at the model's window, less `--max-tokens` (room for one reply), less 10% of the window. For OpenAI's 1,050,000-token models and 32,000-token replies, that's 913,000 tokens. The old fixed 150,000 suited a 200,000-token model (the formula gives 148,000 there), but compacted a million-token model at 15% of its window.
*Your choices:* the formula above. If a model's window isn't known, Wren never compacts by itself; a cap still applies if one is set. `--context-budget` / `WREN_CONTEXT_BUDGET` stays as that cap: it can only lower the budget, and 0 means never compact by itself. OpenAI's numbers come from its model pages, downloaded and read rather than recalled.
*Sources (evidence, not assertion):* Anthropic's API reports each model's window (`max_input_tokens` from `models.retrieve`), so Wren asks it. OpenAI's API doesn't (its `Model` has only id, created, owner and shutdown date), so `DOCUMENTED_CONTEXT_WINDOWS` is a table copied from `developers.openai.com/api/docs/models/<model>`, checked 12 October 2026: all ten models list a 1,050,000-token window and 128,000 max output. `/usage` shows the window with its source beside it.
*Design details:* the agent asks once per (provider, model): at the start of a turn, and in the app from a worker at start-up and after a model change, so the bar appears before the first message. A failed lookup stores nothing and is retried next turn; an answer that arrives after the model changed is dropped. With no budget, the footer says why instead of a bar ("window not looked up yet", "window unknown: never compacted", "compaction off"), and `/usage` spells out how the figure was worked out. The REPL gets the `/usage` lines; the footer is the app's only.
*Not chosen:* a flat 90% of the window (ignores the reply, which can be 32,000 tokens or more). Compacting right at the window less the reply (one long turn of tool output overflows it). Falling back to 150,000 for an unknown model (a made-up number presented as the model's).
*Cost:* long conversations now run far longer before compacting, and every request re-sends them. Several models charge more for long prompts (D89: Haiku 5.5 over 100k, several OpenAI models over 272k), so those prices now apply more often. A cap brings back the old behaviour: `--context-budget 150000`.

### D100. Auto mode: contained actions go ahead without asking
*Step 9 · you*

You said you'd stopped reading the permission prompts, and asked for an auto mode. Of four options you chose a sandbox plus auto-approval, the way Codex CLI works. `/auto` turns it on: a request the tool marks `contained` is allowed without a question. That means an edit to an ordinary file in the project, or a command that will run in the sandbox. Everything else still asks. Wren works out containment from the resolved path or from where the command will run. It is never the model's claim (journal 09, Idea 3).
*Not chosen:* a reviewer model judging each action (what you asked about: neither Claude Code's nor Codex's classifier is offered as an API as far as we could find, and a reviewer reads the same files the agent does, so text in them can try to sway it). Approving everything (simplest; protects nothing). The sandbox now and a reviewer later for commands that leave it (still open, as a follow-up).
*Cost:* without a sandbox (anything but macOS), auto mode still asks before every command.

### D101. On macOS, every command runs in the sandbox, in normal mode too
*Step 9 · design, confirmed by you*

The sandbox applies whether auto mode is on or off. The permission question says "in the sandbox" or "outside the sandbox".
*Why:* the same command behaves the same way in either mode, and what you approve is what runs. It's also how Codex works: its sandbox doesn't depend on its approval setting.
*Not chosen:* sandboxing only in auto mode (normal mode would keep step 3's behaviour, with no first attempt that fails for lack of network, but an approved command would do different things depending on a mode you may have forgotten you set).
*Cost:* a command that needs the network or writes `.git` fails once in the sandbox. The model then asks again with `outside_sandbox` (D104), which costs a model call and a question.

### D102. Wren writes its own Seatbelt profile, denying by default
*Step 9 · design*

`tools/sandbox.py` builds a profile of about 40 lines of SBPL and runs commands with `sandbox-exec -p`. It starts from `(deny default)` and allows what terminal programs need, with a short list of system services taken from Codex's and Anthropic's sandbox-runtime's profiles (both Apache-2.0). Paths go in as parameters (`-D NAME=path`), never pasted into the profile's text, and as real paths (`/tmp` is `/private/tmp`).
*Why deny-default:* under `(allow default)` a command can ask the system to open an app, and the app runs outside the sandbox. Checked: `open -a Calculator` works under an allow-default profile and fails under Wren's.
*Not chosen:* Anthropic's `@anthropic-ai/sandbox-runtime` (it also does Linux and a network allow-list, but it's a Node package, and Wren is Python). Docker or another container (a separate install, slow to start, and the project has to be mounted in). Bubblewrap or Landlock (Linux only; a later step).
*Cost:* macOS only. Apple marks `sandbox-exec` as deprecated, though it ships it and Codex and Claude Code rely on it. The profile is ours to maintain.

### D103. What a sandboxed command may do
*Step 9 · design*

- **Read** anywhere, except the credential folders in your home directory (`.ssh`, `.aws`, `.gnupg`, `.netrc`, `.config/gh`, `.docker`, `.kube`, `.pypirc`, `.npmrc`, `Library/Keychains`). With no network they couldn't be sent anywhere, but output goes to the model and into the session file.
- **Write** inside the project and the temporary folders, except `PROTECTED` at the top of the project: `.git`, `.envrc`, `.vscode`, `.idea`, `.claude`, `.codex`. These are run later, outside the sandbox, by git, direnv, editors or other agents. So `git status`, `log` and `diff` work in the sandbox, and `git add` and `commit` don't.
- **No network.**

*Not chosen:* no reads outside the project (stricter, but it breaks toolchains and settings under your home directory: `~/.gitconfig`, `~/.pyenv`, caches). A network allow-list through a proxy, as sandbox-runtime does (a larger piece of work; for later). Protecting those names at every depth (Seatbelt can only match a path relative to a parameter by its start, and building a regular expression would mean pasting the path in).
*Cost:* a command can still read other projects and your shell history, and write `sub/.envrc`.

### D104. Leaving the sandbox is the model's request, asked about every time it isn't "always" allowed
*Step 9 · design*

The shell tool has an `outside_sandbox` argument, false by default. Setting it runs the command unsandboxed, and the request is never contained, so auto mode asks about it too. Its "always" scope is `shell-outside:<word>`, separate from the sandbox's `shell:<word>`, so "always allow `uv`" in the sandbox doesn't cover `uv` with the network. When a sandboxed command fails, its result says it ran in the sandbox and how to ask to leave.
*Not chosen:* Wren noticing a sandbox refusal and retrying outside by itself (that's leaving the sandbox without asking). No way out at all (installing packages and committing would be impossible).
*Cost:* the model decides when to ask. A model that sets `outside_sandbox` on everything turns auto mode back into a question per command, which is safe but tiresome.

### D105. In auto mode, edits to files that run by themselves still ask
*Step 9 · design*

An edit or write is contained unless any folder or file in its path is one of `PROTECTED`, at any depth (`sub/.vscode/tasks.json` counts). The writing tools still refuse `.git` entirely (D30).
*Not chosen:* the sandbox's top-level-only rule (D103), which would let an edit to `sub/.envrc` through. Protecting memory files too (they can't grant permission, D53, but they do steer the next conversation: listed as a known limit, and an easy addition if you want it).

### D106. The prompt and tool descriptions say the user "may" be asked
*Step 9 · design*

Auto mode switches on and off mid-conversation, but the system prompt and the tool list are fixed for a conversation (invariants 16 and 17). So they now say the user may be asked to approve an edit or command, which is true in both modes. The shell's description describes the sandbox only on a machine that has one, which can't change mid-conversation either.
*Not chosen:* telling the model when auto mode changes (it would need a message in the history, and nothing about the model's job changes: it should be as careful either way).

### D107. `/auto` is a toggle for the session, shown in the footer
*Step 9 · design*

`/auto` switches auto mode on or off, in both front ends. The app's footer shows `auto: on` in yellow, and `/permissions` describes what auto mode allows. Like "always" answers (D23), it's forgotten on exit and isn't restored with a session.
*Not chosen, for now:* a `--auto` flag or `WREN_AUTO` (you want one, as a separate piece of work: [issue #9](https://github.com/bensullivan2002/wren/issues/9)). A saved default (a setting that removes questions in every project, including repositories you've only just cloned).

### D108. CI on GitHub Actions: lint, a dependency audit, and the tests on Linux and macOS
*[Issue #11](https://github.com/bensullivan2002/wren/issues/11) · you (the checks and the matrix) · design (the details)*

`.github/workflows/ci.yml` runs on every pull request, every push to `main`, weekly, and on demand. Its jobs: lint and format (the pre-commit hooks, D112), a dependency audit (D111), and the tests on Ubuntu and macOS, each with Python 3.10 and 3.14. It installs from `uv.lock` (`--locked` fails if the lock is out of date), so CI tests the versions you develop against.
*Why macOS:* the sandbox's tests (step 9) skip anywhere else. *Why two Pythons:* the oldest Wren supports catches newer syntax slipping in, and the newest catches deprecations; the ones between rarely fail alone. Runs are free for a public repository, macOS included.
*Design details:* the token is read-only. Actions are pinned to a commit, with the release in a comment, because a tag can be moved to other code. A newer push cancels the run it replaces. Jobs time out after 10 or 15 minutes (a hung UI test would otherwise hold a runner for six hours), and `faulthandler_timeout` prints where a hang is.
*Not chosen:* every Python from 3.10 to 3.14 (ten test jobs for little more). Linux only (the sandbox untested). tox or nox (a second way to describe environments, when uv has one).
*Checked on the first run ([PR #14](https://github.com/bensullivan2002/wren/pull/14)):* GitHub's macOS runners allow `sandbox-exec`. All 402 tests ran there, none skipped; Ubuntu skips the 6 that need the sandbox.

### D109. Ruff lints and formats, at its defaults; the code was reformatted once
*Issue #11 · you*

Ruff's formatter (Black's style, 88 columns) and its default lint rules, plus bugbear (`B`, likely bugs) and bandit (`S`, security). Ruff's version is pinned in `.pre-commit-config.yaml`. The reformat changed 40 Python files and is a commit of its own, listed in `.git-blame-ignore-revs`, so blame shows the change behind a line rather than the reformat.
*Why:* one fast tool in place of Black, isort, flake8 and bandit. 88 columns is the common default, and short lines read better on a phone.
*Not chosen:* 120 columns (closer to how the code was written: 29 files reformatted rather than 40). Linting without a format check (no churn, but nothing keeps the style consistent).
*Cost:* a large one-off diff. Merge with a merge commit, as usual: a squash or rebase changes the hash the ignore file names. Ruff's default rules change between versions, so upgrading it (`pre-commit autoupdate`) can bring new findings.

### D110. Lint findings are fixed, or ignored on their line with a reason
*Issue #11 · you (the rule) · design (each case)*

Ruff found 102. Ruff fixed 41 itself (import order, and `typing` imports that now live in `collections.abc`). With its more cautious fixes, 28 unpacked-but-unused test variables became `_name` and 2 `[...][0]` became `next(...)`. 14 were fixed by hand: `raise ... from e`, `zip(..., strict=True)`, `ClassVar` on Textual's `BINDINGS`, `X | Y` for `Union[X, Y]`, an explicit `check=False`. The other 17 are deliberate, and say why with `# noqa: CODE - reason`:
- 11 broad `except Exception`, each at a boundary where any failure must be reported rather than crash Wren: a provider's SDK, a worker thread, a tool (D18).
- The shell tool running its command (its job, and you approved it), `/tmp` among the sandbox's writable folders (D103), the exchange rate's fixed https URL, `git` found on the PATH, and two asserts that state an invariant.

Tests and the logo generator are exempt from bandit's rules: they assert, run commands and use fixed paths on purpose.
*Not chosen:* switching rules off project-wide (new code would go unchecked). Narrowing the broad catches (an SDK's exceptions aren't a closed list).

### D111. Security: bandit's rules, a dependency audit, and Dependabot
*Issue #11 · you (what) · design (how)*

- Ruff's `S` rules (bandit's checks) on `src/` (D109, D110).
- `pip-audit` checks every package in `uv.lock`, dev tools included, against PyPI's records of known vulnerabilities. It runs with CI, and weekly so that a newly published one shows up in a quiet week.
- Dependabot opens a weekly pull request for the Python packages and another for the actions. It waits a week after a release before proposing it, since a hijacked release is usually pulled within days.

*Repository settings, not files (you chose them all, as long as they're free; they are, on a public repository):* CodeQL's default setup, Dependabot alerts and security updates, switched on with this step. Secret scanning with push protection was already on, as it is by default for public repositories.
*Not chosen:* the standalone `bandit` (ruff's `S` rules are the same checks, without a second tool). The hook versions in `.pre-commit-config.yaml` aren't left to Dependabot: `pre-commit autoupdate` updates them, deliberately (D109's cost).

### D112. pre-commit runs the quick checks on each commit; CI runs the same list
*Issue #11 · you*

`.pre-commit-config.yaml` lists ruff (lint with fixes, then format) and some hygiene checks: trailing whitespace, a final newline, valid YAML and TOML, leftover merge-conflict markers, files over 500 kB. After `pre-commit install` they run on the files in each commit, and CI's lint job runs them on every file. It's one list, so your machine and CI can't disagree.
*No tests in the hook:* a hook should take a second or two, or it gets skipped (`--no-verify`). The tests run in CI.
*Not chosen:* pre-commit.ci (a hosted service that pushes fixes to pull requests: an outside app with write access). A hand-written git hook (no pinned versions, nothing shared with CI).

### D113. Ruff leaves Markdown alone
*Issue #11 · design*

Ruff 0.17 also formats Python code blocks inside Markdown, and wanted to rewrite the snippets throughout `docs/`. `extend-exclude = ["*.md"]` stops it.
*Why:* the snippets are laid out by hand for reading (aligned comments, short calls, `...` for what's left out), and each journal entry records its step as it was. Reformatted, they were longer and no clearer.
*Not chosen:* formatting them (consistent with the code, but it rewrites finished entries).
*Cost:* a snippet's style can differ from the code's. That was already true, since snippets are excerpts.

### D114. Locally, run the tests for what changed; CI runs them all
*Issue #11 · you*

A session runs the tests for the code it changed (the matching `tests/test_<module>.py`, or `-k`) and leaves the full suite to CI, checking `gh pr checks` before merging. It saves the time, and the tokens, of running and reading the whole suite each time. CI catches what a narrow run misses, as long as nothing merges with CI failing. So `main` is protected: a pull request can't merge until all six CI jobs pass. You can still override it as an admin, and a branch needn't be up to date with `main` first (one person working rarely needs that, and it costs a rerun per merge). The dependency audit is required too: a vulnerability newly published for a dependency blocks merging until it's updated, which is the point.
*Not chosen:* `pytest-testmon`, which records which tests run which code and picks the tests itself (automatic, but another dependency and a data file, and it misses what it can't trace, such as a file a test reads).

### D115. Type checking will use basedpyright, as its own piece of work
*Issue #11 · you*

Not part of this step: how much code it would ask to change hasn't been measured. When it comes, it's another CI job, and probably a hook. The questions to settle are in [issue #15](https://github.com/bensullivan2002/wren/issues/15).

### D116. The sandbox protects `.git`, `.claude` and the rest in every folder of the project
*Follow-up to step 9 · you (worktrees get the same rules, and first) · design (how)*

Worktrees moved from `.claude/worktrees/` to `.worktrees/`: the sandbox protects `.claude` as a whole, so every write in a worktree there needed `outside_sandbox` and a question. You asked for worktrees to get the same sandbox rules as the working directory. A worktree is a checkout of its own, with its own `.git` file (which says where its history is), `.claude`, `.envrc` and `.vscode`, and D103 protected those names only at the project's top level. Now a sandboxed command can't write any of `PROTECTED` in any folder of the project, which is the rule the file tools already used for auto mode (D105).
*How:* `subpath` matches a path by its start, so this rule is a regular expression. D103 turned that down because building one seemed to mean pasting the path into the profile. It doesn't: SBPL's `string-append` joins the project's path, passed as a parameter (`ROOT_ESCAPED`, escaped with `re.escape`), to a fixed pattern built from `PROTECTED`, inside the profile. Checked on macOS: folder names with spaces, quotes, brackets, `$`, `\` and `#` work; `mv` and `ln -s` onto a protected name are refused; and on a case-insensitive disk Seatbelt refuses `.Claude` and `.GIT` too. Only names *below* the project count, so Wren started inside an old `.claude/worktrees/` checkout can still write its files.
*Not chosen:* finding the worktrees (from `.git/worktrees/*/gitdir`) and protecting each one's top level (exact, but misses `sub/.envrc`, submodules and other checkouts inside the project). A pattern without the project's path, limited to the project with `require-all` (simpler, but a project inside a folder named `.claude` couldn't be written at all).
*Cost:* `git add` and `git commit` in a worktree still need `outside_sandbox`, as they do in the main checkout: both write the main checkout's `.git`. A folder moved whole into the project from a temporary folder (`mv /tmp/x sub`) isn't looked inside, because Seatbelt checks the two paths of a move, not what the folder holds. `git init sub` inside the project now needs `outside_sandbox`.

### D117. Commands in the sandbox never ask, in either mode
*Follow-up to step 9 · you*

You were approving too many commands, and asked for commands in the sandbox to run without a question. Of three options (never ask in the sandbox, in any mode; keep `/auto` and add the `--auto` flag, issue #9; a saved setting) you chose the first, after D116. The shell's `permission()` now returns None for a command that will run in the sandbox, as a read-only tool does, so nothing is asked and nothing needs allowing. A command outside the sandbox (`outside_sandbox`) is asked about as before, in both modes. Without a sandbox (anything but macOS) every command still asks. Auto mode now differs from normal mode only in edits to ordinary files.
*Why:* the sandbox is enforced by the operating system, and a question about what it already fences mostly trains you to press `y` (D100's reason for auto mode). It matches the defaults of Claude Code (sandboxed commands allowed automatically while its sandbox is on) and Codex CLI (commands run in its workspace sandbox, and it asks only to leave it), as Wren's author remembers their documentation; not re-checked for this decision. The command still shows in the conversation, with its description, as it runs.
*Design details:* returning None, rather than a request marked as allowed, keeps `contained` to one meaning (an ordinary file edit, for auto mode), and `permission()` and `run()` still decide sandboxing with the same `_sandboxed` (invariant 26). The `shell:<word>` scope remains only where there's no sandbox. `/permissions` says that commands in the sandbox run without asking, and `/auto` that it adds edits.
*Not chosen:* allowing them through `Permissions` with a new flag on the request (a request built only to be waved through). Keeping the question in normal mode (where you started).
*Cost:* in normal mode, a sandboxed command can now change any ordinary file in the project without a question, while `edit_file` asks before the same change. What it writes for later (a test, a `Makefile`, `WREN.md`) runs or is read outside the sandbox later, so read `git diff` before running or committing. When Wren runs Wren's own tests, the sandbox's tests fail, because a sandbox can't start inside another (journal 09); the model has to run them with `outside_sandbox`.

### D118. A ChatGPT plan is a way to pay for OpenAI, not a provider
*Step 10 ([issue #19](https://github.com/bensullivan2002/wren/issues/19)) · you*

`--auth chatgpt` (or `WREN_OPENAI_AUTH=chatgpt`) makes the OpenAI provider pay from your ChatGPT plan; `--auth api-key`, the default, keeps your API key. Same provider, model, API and translation: `OpenAIProvider` takes a `plan` (a signed-in `ChatGPTSession`), and the plan route's own rules live in `providers/openai_plan.py`. Wren keeps its own agent loop, history, tools and permissions.
*Why:* the issue asked to keep choosing a provider separate from authenticating, and the code agrees: both routes send the same items and read the same replies.
*Not chosen:* a separate `chatgpt` provider (less code, but it ties "which model" to "who pays"). Codex's SDK or app-server (a vendor's agent loop, which the issue rules out: Wren is for learning by building, and stays provider-agnostic).

### D119. Who pays is chosen once, at startup, and nothing in Wren changes it
*Step 10 · design, from the issue's rule*

The route comes from the flag or the environment variable only. It isn't a saved default (D85 and D86's `settings.json`), and on the plan route the provider has no API-key client at all (`self.client = None`). A failed request, a usage limit or a failed refresh stops the request with an error; nothing retries it another way. The agent carries the route (`agent.auth`), so `/provider` and resuming a session rebuild OpenAI the same way: if you're not signed in, switching to OpenAI fails rather than using `OPENAI_API_KEY`. `--auth` affects OpenAI only.
*Why:* the issue's rule ("never silently fall back to a paid API key, another provider, or purchased usage; switching billing routes must be explicit"). A flag or a line in your shell profile is something you can see you did; a saved setting isn't.
*Not chosen:* saving it with the model (convenient, but invisible). Passing the route through both front ends and `run_command` (correct too, but a parameter threaded through half a dozen signatures to reach one call).
*Cost:* `agent.auth` is a field the agent holds but doesn't use.

### D120. PyJWT checks the ID token's signature
*Step 10 · you*

A new dependency, `pyjwt[crypto]` (which brings `cryptography`). `verify_id_token` fetches OpenAI's published keys (from the OpenID configuration's `jwks_uri`), picks the one the token's `kid` names, and checks the signature, issuer, audience (Wren's issued client ID), expiry (with 60 seconds' leeway) and that `sub` is there; then Wren compares the nonce. The algorithm comes from OpenAI's key, never from the token's header, and only public-key algorithms are accepted, so the HS256 forgery fails (journal 10, Idea 4).
*Not chosen:* `authlib` (a whole OAuth framework, for one check). `joserfc` (fine, less used). Checking RSA signatures by hand (not safe to write ourselves).

### D121. `wren login`, `wren logout`, `wren auth-status` are terminal commands
*Step 10 · you*

Subcommands of `wren`, like `codex login`. They print and exit; `wren` with no command starts a conversation as before.
*Why:* sign-in opens a browser and waits, which needs no screen of its own, and works the same with or without the full-screen app.
*Not chosen, for now:* `/login` inside the app, as Claude Code does (more UI work: a follow-up).

### D122. The sign-in is kept in `~/.wren/auth/`, owner-only, out of the sandbox's reach
*Step 10 · you (the place) · design (how)*

`chatgpt.json` holds the record OpenAI's docs suggest (identity, issued client ID, host ID, tokens, granted scopes, when they arrived); `host.json` holds the host ID. The folder is `0700`; files are written `0600` from creation (`mkstemp`), then renamed into place, so a crash never leaves half a record. Tokens have `repr=False`. `.wren/auth` is added to the sandbox's `CREDENTIALS` (D103), so a sandboxed command can't print them to the model. Codex's credential files are never read or written.
*Not chosen, for now:* the macOS Keychain (the `keyring` package; a follow-up). Keeping the tokens in Wren's process only (you'd sign in every time).

### D123. One ChatGPT account for now
*Step 10 · you*

Wren keeps one registration. Signing in again reuses it; signing in as a different account is refused, and nothing is replaced. The record is keyed by its issued client ID, so more accounts can be added later.
*Cost:* to switch accounts, delete `~/.wren/auth/chatgpt.json` and sign in again. The old registration stays at OpenAI until you disconnect it in ChatGPT's settings.

### D124. The sign-in's own requests use the standard library
*Step 10 · design*

The token, key, discovery and revocation requests (a handful of small JSON requests) use `urllib.request`, behind a two-method `Http` protocol that the tests replace. Only `https` addresses are opened. Inference still goes through the OpenAI SDK, with the access token as its `api_key`, as OpenAI's docs show.
*Why:* the SDKs now use `httpx2` rather than `httpx`, and depending directly on a library for four requests would be one more thing to keep up with.
*Cost:* a little more code than `httpx` would need.

### D125. The host ID is a random `urn:uuid`
*Step 10 · design*

Made once per computer, before the first sign-in, and kept in `host.json`, which signing out doesn't touch. It identifies the computer to OpenAI and nothing else, so it says nothing about you.
*Not chosen:* the docs' recommended JWK thumbprint (it needs a key pair Wren has no other use for, and OpenAI doesn't check possession of the key yet).

### D126. Refreshing: five minutes early, one at a time, read again after the lock
*Step 10 · design*

`ChatGPTSession.access_token()` runs before every request (and model list). It refreshes when the token has under five minutes left, holding `flock` on `chatgpt.lock`, and re-reads the record once it has the lock, so a refresh by another Wren is used rather than repeated (journal 10, Idea 6). Access token, expiry, scopes and the rotated refresh token are saved together, in one write; the verified ID token is kept as it was. `earliest_refresh_at` is honoured when it's a number (its format isn't documented). A dead refresh token clears the tokens but keeps the registration; a network fault or 5xx keeps everything. Signing out takes the same lock, tries revocation three times (immediately, then after 1 and 3 seconds) on a network fault or 5xx, and always forgets the tokens, saying whether OpenAI confirmed it.
*Not chosen:* refreshing on a 401 and retrying the request (a 401 here can also mean a missing permission, and a retry is a second request you didn't ask for). A lock inside the process only (two terminals would race).

### D127. The plan route's requests follow its documented limits
*Step 10 · design*

`store: false` and `stream: true`; the whole history in `input`; no `max_output_tokens` (refused); the system prompt in `instructions`; Wren's tools in one namespace, `wren`, with `namespace: "wren"` on tool calls sent back; `include: ["reasoning.encrypted_content"]` and effort as on the API-key route. The SDK client is made per request with `base_url` fixed to the public API and `max_retries=0`. The stream's events are read directly: only `response.completed` (or `response.incomplete`, a partial reply) gives a reply; `response.failed`, an `error` event, or a stream that just ends, raise. The model list is the account's own (`GET /models`, entries with `visibility: "list"`, in the server's order).
*Not chosen:* the SDK's `responses.stream` helper (used on the API-key route, unchanged: it assembles the reply itself, which leaves less room to tell a failure after streamed text from a cut-off stream). Per-tool namespaces (more shapes to get wrong, for nothing).
*Cost:* `--max-tokens` has no effect on this route. The namespace round trip is the part most likely to differ from the docs; it's first on the list to try.

### D128. Plan-usage errors say what happened and what to do, and stop
*Step 10 · design*

Each documented error code (`subscription_sharing_usage_limit_exceeded` and the rest) has a sentence of advice in `openai_plan.ADVICE`; an admission refusal with only `{"detail": ...}` gets advice by HTTP status, with the detail shown as text. Underneath: the HTTP status, code, `param` and request ID, and "Wren didn't switch to an API key or anything else". Limits link to https://chatgpt.com/settings/usage.
*Not chosen, for now:* automatic retries with backoff for the temporary ones (`…_unavailable`, 503): a follow-up. Today you send the message again.

### D129. Who pays is shown beside the provider, everywhere
*Step 10 · design*

Every provider has a `billing` ("API key", "ChatGPT plan", "free"). The footer shows it in the same part as the provider, `provider: openai (ChatGPT plan)`, so one is never shown without the other, at any width. The REPL's first line says `billing: …`; `/usage` says "Paid for by" in a sentence (`billing_note`), with the signed-in account and the usage link. The plan route has no price (`prices()` is None), so its calls show as unpriced.
*Not chosen:* a separate `billing:` part in the footer (tried first: at 80 columns the footer dropped the provider to fit it, and the two could be shown apart).

### D130. Asking for plan permission again uses `prompt=consent`
*Step 10 · design*

If the saved sign-in lacks the plan permission (you declined it), `wren login` asks OpenAI to show the consent screen again (`prompt=consent`), with the full set of scopes. An ordinary sign-in never forces it. The docs' newer `force_reconsent=true` waits until OpenAI confirms it's deployed for Wren.

### D131. On the plan route, the reply is read from the finished-item events
*Step 10, found in your first real run · design*

Your first test with a real account saved every reply as `(no reply)`: text streamed, but tool calls never ran. A probe of the live route (13 October 2026) showed why. Its `response.completed` carries the usage but an **empty** `output`, and each item (message, tool call, reasoning) arrives once, finished, in a `response.output_item.done` event. `read_stream` now collects those by `output_index` and uses them when the final event has none. A reply that spent output tokens but yielded no item Wren can read is now an error ("a bug in Wren"), not a silent empty reply. Checked live afterwards: a `list_files` call came back as `function_call` with namespace `wren`, ran, and the request carrying its result was accepted (D127's namespace round trip).
*Not chosen:* always building the reply from the item events (the API-key route and the SDK use the final event; this keeps both shapes working). Rebuilding text from the deltas (it loses tool calls and reasoning). The SDK's own recovery, which only covers a final event with *no* `output` field, not an empty one.
*Lesson:* the fakes were built from the docs, and the docs' example only waits for `response.completed`. The tests' fake stream now has the live shape.

### D132. Steering and remembered context are separate files
*Issue #17 · you (the separation and write target) · design (the loading and prompt rules)*

`WREN.md` (or else `AGENTS.md`) holds standing instructions; `MEMORY.md` holds remembered facts, lessons and notes. One loader keeps separate lists and renders separate prompt sections. Project discovery remains repository-root-to-working-directory, with the existing size cap and unreadable-file handling. `~/.wren/WREN.md` remains personal steering; there is no personal memory file. Explicit steering wins over conflicting memory, and the user's requests win over both. Neither kind grants permission.

`/remember` always appends to working-directory `MEMORY.md`, creating it if missing. It never edits steering or ancestor memory. The fixed prompt gives the model the same destination even when no files exist, and forbids saving notes unprompted. Both kinds remain fixed per conversation and survive compaction; changes take effect on clear, restart or resume. Startup, `/memory`, and the footer distinguish the kinds.

*Supersedes:* D48's conflation of steering with memory, D49's write destination, and D54's nearest-file write rule. The steering fallback and the cache, discovery-boundary and permission rules remain.
*Why:* remembering a fact should not modify standing instructions or shadow `AGENTS.md`. A working-directory target is predictable and accessible to the ordinary edit tools, which cannot write ancestor files.
*Not chosen:* writing to the nearest ancestor memory (the old target-selection shape); automatic migration of mixed files (we cannot reliably distinguish instructions from notes); auto memory or cross-project personal memory (separate trust and scope decisions). Existing `WREN.md` and `AGENTS.md` still load as steering; users can move old notes themselves.

### D133. GitHub reads use a dedicated tool, not a network whitelist for shell
*Issue #12 · you (dedicated read-only tool) · design (request and execution boundaries)*

`github_read` performs fixed REST GETs to GitHub.com via the user's installed, authenticated `gh api`. Inputs select an operation, explicit repository, optional issue/PR number and bounded page; they cannot select a host, endpoint, method or CLI flags. Arguments are executed directly without a shell. Reads run without a prompt in either mode and on every platform. Every GitHub write, including comments and creation, keeps the existing shell approval flow. No blanket `gh` permission is added, and the sandbox still blocks all network and gh credentials.

*Why:* the issue asks for approval-free reads, not unrestricted GitHub access. A domain whitelist would also permit destructive requests to that domain. The existing first-word “always” scope is too broad to establish read-only access.
*Not chosen:* parsing and allowing selected `gh ...` shell strings (a shell parser and complete per-command flag policy become a security boundary); arbitrary REST GET endpoints (a larger surface); GraphQL (queries and mutations share POST); a new GitHub token store (gh already handles sign-in).
*Design details:* the executable must resolve outside the writable project and temporary folders. HTTP debug logging is disabled in a copied environment. Collections use explicit pagination (1–100 items per page), output is truncated at 20,000 characters, and a single timeout covers all requests. PR checks combine check runs and commit statuses for one validated head SHA; comments combine conversation and inline review comments. Existing user-granted “always gh” rules are not revoked or broadened.
*Limits:* GitHub.com only; no arbitrary endpoint or GraphQL; the issue list includes PRs; full JSON can truncate, and huge diffs cannot be paged by this tool. The installed CLI/configuration are trusted local software. Private repository content is sent to the model and saved in the session, just like file contents. The shared runner buffers output before applying the display cap.

### D134. Make the footer readable without making it taller
*Issue #34 · you (a more colourful, readable footer) · design (styling)*

Cyan labels replace dim labels; headline values are bold, spending is magenta, and shortcut keys are highlighted separately from their descriptions. Yellow remains a warning (auto mode, always-allowed actions, stopping) and red an error or high-context signal. Separators and incidental metadata stay dim. A theme-aware surface and text colour distinguish the footer from the transcript without extra rows. Rich `Text` styling preserves literal external text and the existing priority-based fitting.
*Not chosen:* borders, extra headings or blank rows (less room for the conversation, especially at 80×24); changing the information or fitting priorities (the issue likes the existing information); colouring every value (warnings and headline figures would no longer stand out).
