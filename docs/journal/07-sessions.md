# 07 · Sessions, and switching models

*Step 7 · October 2026*

## What we built

Quitting Wren no longer throws the conversation away.

- **Every conversation is saved as it happens,** to a file under `~/.wren/sessions/`.
- **`wren --resume`** opens Wren on a list of this folder's earlier conversations. **`/resume`** opens the same list from inside. **`wren --resume ID`** goes straight to one.
- A resumed conversation is redrawn on screen, tool calls and all, and carries on in the same file.
- **`/model`** opens a list of models to switch to, mid-conversation. `/model NAME` switches directly.
- **`/effort`** chooses how hard the model works on each reply, where the model has such a setting.

Try it without an API key:

```bash
cd /tmp/play && wren --provider fake
> remember the number 42          # then Ctrl+Q
wren --provider fake --resume     # pick it from the list
> /model                          # pick another model
```

Then look at what was written: `ls ~/.wren/sessions/*/` and `cat` one of the files.

---

## Idea 1: a log, not a snapshot

The obvious way to save a conversation is to write the whole history to a file whenever it changes. Wren doesn't. Its session file is **JSON Lines**: one small JSON object per line, and saving means adding a line to the end.

```json
{"type": "meta", "version": 1, "cwd": "/tmp/play", "created": "2026-10-12T14:02:11", "provider": "fake", "model": "demo"}
{"type": "message", "role": "user", "content": [{"type": "text", "text": "remember the number 42"}]}
{"type": "message", "role": "assistant", "content": [{"type": "text", "text": "You said: remember the number 42"}]}
```

Nothing already written is ever touched. That gives three things a rewritten file can't:

- **There's no "save" to forget.** Each message is on disk before the next thing happens. A test checks that the user's message is in the file after a single event of the turn.
- **A crash costs at most one line.** Power cut, `kill -9`, a bug: everything before the line being written is intact. A file being rewritten in full, interrupted half-way, is destroyed.
- **It's cheap.** Appending a line doesn't get slower as the conversation gets longer.

The file is a **log of what happened**. To get the history back you replay it from the top (`sessions.load`). That's what lets a compaction be recorded without rewriting anything: it's one more line, of type `replaced`, carrying the new two-message history. Replay reaches that line and swaps what it has built so far.

Step 3 made the history truthful, step 5 made it append-only for the cache's sake, and here the same property turns up a third time: **an append-only history is one you can log by appending.** Databases work this way too (they call it a write-ahead log).

## Idea 2: the agent is told as little as possible

The agent doesn't know about files. It has a `journal`: any object with five methods.

```python
class Journal(Protocol):
    def append(self, message): ...          # a message was added
    def replace(self, messages): ...        # the history was replaced (a compaction)
    def model_changed(self, provider, model): ...
    def spent(self, usage, context_tokens): ...   # a model call finished
    def reset(self): ...                    # the conversation was cleared
```

For that to be reliable, the agent must report *every* change. There were six places that appended to `self.messages`. Now there is one:

```python
def _add(self, message: Message) -> None:
    """The one way a message joins the history. Because every addition
    comes through here, the journal can't miss one."""
    self.messages.append(message)
    if self.journal is not None:
        self.journal.append(message)
```

When something must happen every time, don't rely on remembering it in six places. Make one door and send everything through it.

## Idea 3: a file format is a promise

`sessions.py` turns each message into JSON by hand, block by block, when a library could do it automatically from the dataclasses. That's deliberate. Today's code will read files written months ago by an older Wren. If the format were derived from the Python classes, renaming a field would silently make every old session unreadable.

So the names in the file are written down explicitly, and the file starts with `"version": 1`. Two rules in `load` protect the future:

- **An unknown line type is skipped,** not an error. An older Wren can read a newer file's messages and ignore what it doesn't understand.
- **A higher version number is refused,** with a clear message. That's for a change old code truly can't survive.

## Idea 4: where the files live, and who can read them

A transcript contains whatever the model read: your source, command output, sometimes a secret printed by a careless command. So:

- They're under `~/.wren/sessions/`, **not in the project**. A `git add .` can't publish them.
- The folder is created with mode `0o700` and each file with `0o600`: readable by you and nobody else on the machine.
- They're grouped by project. The folder name is the project's path with punctuation turned into dashes (`/Users/ben/code/wren` becomes `-Users-ben-code-wren`), so you can find one by eye. `--resume` and `/resume` only show the current folder's.

## Idea 5: a saved conversation can stop anywhere

Wren might be closed politely between turns. It might also be killed while a tool was running, leaving a file that ends with a tool request and no result. Load that as-is and the API rejects every request (step 2's rule: every `tool_use` needs its `tool_result`).

Step 3 already solved this for Ctrl-C, by tying off the loose ends. Resuming reuses the same code:

```python
def restore(self, messages):
    previous = list(self.messages)
    self.messages[:] = messages
    if self.messages:
        self._tie_off(RESUMED_NOTE, finished={}, not_run=NEVER_RECORDED)
    problems = history_problems(self.messages)
    if problems:
        self.messages[:] = previous
        raise ValueError("the saved conversation is not usable: " + "; ".join(problems))
```

A pending tool call gets *"No result was recorded for this tool: Wren closed while it was pending. It may or may not have run: check before relying on it."* That wording matters. We don't know whether the command ran, so we don't claim either way, and we tell the model to find out.

Then `history_problems`, the checker from step 2, has the last word. If the history still isn't valid, it's refused and the current conversation is left as it was.

One more thing can be damaged: the last line of the file itself, if Wren died mid-write. `load` forgives a broken *last* line and refuses a broken line anywhere else, because only the first can be explained by a crash.

## Idea 6: what resuming restores, and what it doesn't

| Restored | Not restored |
| --- | --- |
| The conversation, exactly | "Always allow" permissions |
| The model it was using | The state of your files |
| Its token totals and its measured size | |

**Permissions aren't restored on purpose.** "Always allow `pytest` this session" was a decision made in one sitting, about one stretch of work. Coming back days later and finding commands running unasked would be a surprise of the wrong kind (D23, D50).

**Files are whatever they are now.** The model's picture of them is from when the session was saved. The resume note says "Files may have changed since", and the edit tool's exact-match rule (step 3) is the real protection: an edit based on a stale picture fails loudly.

**The system prompt is rebuilt from today's memory files,** not saved. It was never part of the history.

**Token totals and size are restored, from the log.** The first version of this step didn't save them, and a resumed conversation showed `context 0%` and `0 in · 0 out`: you spotted it at once. Neither number can be recomputed from the messages. Step 5's lesson was that only the provider's count is exact, so the fix is to write that count down: one `spent` line after every model call, with what the call cost and how big the conversation then was. Replay adds them up.

Two details. Adding a line type needed no version bump, because old readers skip types they don't know (Idea 3 doing its job within a day). And a conversation that's there but tiny now shows `<1%`, not `0%`, which had read as "empty" or "broken".

## Idea 7: switching models is nearly free to build

```python
def set_model(self, model: str) -> None:
    self.provider.model = model
    if self.journal is not None:
        self.journal.model_changed(self.provider.name, model)
```

That's all of it, because of a decision from step 1: the conversation is Wren's own neutral messages, not any one model's. The provider reads `self.model` on every request, so changing it changes the next request.

Two things don't carry across, and Wren tells you the first:

- **The cache.** It's kept per model. The new model's first reply reads the whole conversation afresh, at full price. After that it's cached again.
- **Private reasoning.** The old model's `ReasoningBlock`s mean nothing to the new one. The docs say it ignores them. The visible conversation, tool calls and results all carry over.

The list of models comes from the API when you open the picker, not from a list in the code, which would be out of date within months.

**How do you know it worked?** The header shows the model Wren will *ask for*. That's Wren's own claim. Every API response also names the model that *answered*, so Wren keeps that (`Completed.model`, taken from the response) and `/usage` shows both:

```
Model asked for: claude-haiku-5-5
Last reply came from: claude-haiku-5-5  (as reported by anthropic)
```

After `/model`, the first line changes at once and the second after the next reply. It's the same idea as the context meter in step 5: when the system will tell you the answer, show that, not your own belief about it.

## Idea 8: effort, and two ways a provider can offer it

Current models can be told how hard to work on a reply: how much to think, how thorough to be. More effort is slower and costs more; less is quick and cheap. `/effort` chooses it.

The interesting part is that providers disagree about what kind of thing effort *is*:

| | Where the setting lives | Example |
| --- | --- | --- |
| **A separate dial** | Sent with the request, beside the model's name | Anthropic: `output_config: {"effort": "high"}` |
| **Part of the model** | The provider lists variants as different models | a "…-low" and a "…-high" in the model list |

Wren only has a Claude provider today, but the `Provider` interface was written so that either kind fits without the rest of Wren caring:

```python
effort: str | None                    # None means "the model's own default"

def efforts(self) -> list[str]:
    """The effort levels the current model accepts as a separate
    setting, lowest first. Empty if it has none."""
```

A provider of the first kind returns levels from `efforts()`. A provider of the second kind returns nothing, puts its variants in `models()`, and `/effort` says: *"…has no separate effort setting. If this provider offers a choice, it's among its models: /model"*. The front ends never ask "which provider is this?". They ask the provider what there is to choose from, and show that. The fake provider plays both parts (`demo-fast` has no levels), so both paths are tested.

Three details:

- **The levels are asked of the API, per model.** They differ (older models take none, newer ones up to five), so `AnthropicProvider.efforts` reads the model's reported capabilities. Nothing is written into the code to go stale.
- **Unset means unsent.** With no effort chosen, the request leaves the field out, and the model uses its own default, which also differs by model.
- **Switching model resets effort to that model's default.** A level the new model doesn't take would make every request fail, and Wren can't know without asking. It tells you.

Like switching model, changing effort resets the cache for one request, and Wren says so. A session records its effort and resuming restores it.

### What can be proved about effort, and what can't

For the model there is proof: the response names the model that answered. You asked for the same for effort, and the honest answer is that there isn't any. Looking through the response types turned up two facts:

- **The response has no field saying what effort was used.**
- **The API doesn't say what a model's default effort is,** only which levels it accepts.

So `/usage` shows what there is, and labels each line with where it comes from:

```
Effort asked for: the model's default, which is medium for claude-opus-5-5  (per Anthropic's documentation, October 2026; the API doesn't report it)
Last request was sent with: no effort setting, so the model's default applied  (Wren's record: anthropic doesn't report the effort it used)
Thinking in the last reply: 1,204 of 1,650 output tokens  (as reported by anthropic; more effort usually means more)
```

Three different kinds of knowledge, kept apart on purpose:

1. **The default** is copied from documentation into a small table in the provider. It will go stale, so it carries its date, and a model that isn't in the table is reported as unknown, never guessed.
2. **What was sent** is read back from the request Wren actually built. It proves Wren asked. It doesn't prove the API obeyed.
3. **Thinking tokens** are the provider's own count of how much of the reply was private reasoning. This is the only measurement, and it's indirect: change effort, send a comparable message, and watch it move.

It would have been easy to print "effort: high (confirmed)". The discipline here is the same as for the model line, turned round: **say what you measured, say what you only believe, and don't let one pass for the other.**

## Idea 9: one picker, three uses

The app has a `Picker` widget: Textual's `OptionList` plus a note of what each row stands for and what to do with the choice. `/model`, `/effort` and `/resume` all use it, and `wren --resume` simply opens the session picker as the app starts. Like a permission question, it sits in the conversation and takes the focus; Escape closes it.

Fetching the model list, or a model's effort levels, is a network call, so it runs on the worker thread (step 4's rule), and the picker opens when the list arrives. The session list is read from disk, which is fast enough to do directly.

On resume the app redraws the conversation from the history: your messages, the replies as markdown, a line per tool call. The one-line summaries (`→ 2 lines`) were never saved, only what the model saw, so a redrawn tool call shows the first line of its result.

## A bug this step found

Adding the redraw made a test hang, and the hang exposed two real faults:

1. **A race at startup.** The app drew the (empty) conversation "after the first refresh". If you typed quickly, that deferred redraw could run *after* your message appeared and wipe it. It's now done before the app accepts input.
2. **A worker that never let go.** If the app closed while a permission question was open, the worker thread waited for an answer for ever, and the program couldn't exit. The app now sets the stop flag whenever it closes, however that happens.

The second had been there since step 4. It took a failing test in a different feature to show it: a thread waiting on something is only safe if *every* way out releases it.

---

## Python worth knowing from this step

- **`json.dumps` / `json.loads`, one per line.** JSON never contains a raw line break (inside a string it's written as `\n`), so one object is always exactly one line.
- **`open(path, "a")`.** Append mode: writes go to the end, whatever else is in the file.
- **`os.open(..., os.O_CREAT | os.O_EXCL, 0o600)`.** Create a file only if it doesn't exist, with given permissions from the first moment. `open()` alone would create it readable by others, then you'd fix it: a small window, but a window.
- **`Protocol`.** `Journal` describes a shape. `SessionStore` never says it "is a" Journal; it just has the methods.
- **`nargs="?"` with `const=""`** in argparse: `--resume` may be given with a value, without one (you get `""`), or not at all (you get `None`). Three states from one flag.
- **`Path.glob("*.jsonl")`** and **`path.stat().st_mtime`**: find files, and when each was last written.
- **`for size, unit in ((86400, "day"), …)`** in `ago`: a table of thresholds, tried largest first.

## Known limits (on the list for later)

- **Sessions are kept for ever.** Nothing deletes old ones; `~/.wren/sessions` only grows.
- **No names or search.** A session is identified by its first message and when it was last used.
- **One folder at a time.** You can't list or resume another project's sessions from here.
- **Resuming one session in two terminals** interleaves both into one file.
- **Transcripts are plain text on disk.** Private to your user account, but not encrypted. Don't paste secrets into a conversation.
- **No way to switch saving off.**
- **Effort is untested against the real API:** the request field and the capability lookup follow the documentation.
- **No other provider exists yet,** so the "effort is part of the model" path is exercised only by the fake one.
- **Switching models is untested against the real API.** In particular, how a different model treats the old one's reasoning blocks is taken from the documentation.
- **A redrawn tool call shows less than the original did:** no diff, no command output, just the first line of the result.
- **Sessions saved before the `spent` line existed** resume with totals at zero and an estimated size, until the first reply measures it.

## Things to try

1. Have a short conversation, quit, and `cat` the session file. Find the `meta` line and one `tool_use`.
2. Start a turn that asks permission, and at the prompt kill Wren from another terminal (`pkill -9 -f wren`). Resume. Read `/history`: what does the pending tool's result say?
3. `/compact`, then look at the file. Is the old conversation still in it? What does that mean for privacy, and for recovering from a bad summary?
4. Delete the last few characters of a session file and resume it. Then damage a line in the middle instead.
5. With a real key: ask something, `/model` to a different model, ask it what you just asked. Then `/usage`: what happened to the cache?
6. In `Agent`, replace one `self._add(...)` with `self.messages.append(...)` and run the tests. Which fail?

## What's next: step 8, extensions

Wren is now a complete single agent: it talks, acts with permission, has an interface, manages its context, remembers instructions and saves its work. Step 8 opens it up: **MCP** (tools provided by other programs, so Wren can use a database or a browser without Wren's code knowing about either), **subagents** (handing a self-contained task to a fresh conversation, so its mess doesn't fill the main context), and **hooks** (your own commands run at set moments, such as after every edit). Each is its own piece of work, and the first decision is which to build first.
