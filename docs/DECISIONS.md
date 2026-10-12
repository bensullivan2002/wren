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
*Cost:* a single very long turn can overshoot the budget. The budget is a cost target, well below the model's real limit.

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
*Branding follow-up · you*

You asked for the logo to be pinned like Claude and Codex, rather than scrolling away. This supersedes D81's placement: a top-docked screen widget appears above the header in every conversation, including resumed ones. `/clear` and history redraws touch only the transcript, leaving the same logo widget in place.
*Design details:* reserve the logo's space outside a flexible body, and reserve the status and prompt outside the body's flexible chat area. The command panel's percentage height is relative to that chat area, so opening `/help` with a multiline input does not push the prompt off-screen. Theme colour, packaged asset, small-terminal fallback, and absence from model history and plain output are unchanged.
*Cost:* the full bird reserves eight rows, plus the existing one-line header. Below 20 columns or 20 rows it becomes the single-line `wren` fallback.
