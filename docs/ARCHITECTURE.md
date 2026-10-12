# Architecture

*Describes Wren as of **step 3**. This page is rewritten as Wren grows, so it always matches the code. For how it got this way, see the [build journal](journal/).*

## The big picture

A language model is, at heart, a function: text in, text out. It has no memory, no hands and no idea where it's running. The **harness** is everything around it that turns that function into something useful: it holds the conversation, talks to the API, shows the output, runs tools on the model's behalf, and decides what the model is allowed to do.

Wren is that harness. `cli.py` builds the parts and wires them together:

```mermaid
flowchart LR
    you([You]) <-->|keyboard / screen| repl["<b>Repl</b><br/>repl.py"]
    repl -->|"run_turn(text)"| agent["<b>Agent</b><br/>agent.py<br/><i>the agent loop</i>"]
    agent -.->|events| repl
    agent -->|"stream(system, messages, tools)"| provider{{"<b>Provider</b><br/>providers/"}}
    provider -.->|stream events| agent
    provider --- anthropic["AnthropicProvider"] --> api[("Claude API")]
    provider --- fake["FakeProvider<br/>(tests, offline demo)"]
    agent -->|"run(name, args)"| registry["<b>ToolRegistry</b><br/>tools/registry.py"]
    registry -.->|ToolResult| agent
    registry -->|"allows(request)?"| perms["<b>Permissions</b><br/>tools/permissions.py"]
    perms -.->|"ask(request)"| repl
    registry --> ro["list_files · read_file · search<br/><i>read-only, never ask</i>"] --> fs[("Your project<br/>(working directory only)")]
    registry --> rw["edit_file · write_file<br/><i>ask first</i>"] --> fs
    registry --> sh["shell<br/><i>asks first</i>"] --> os[("Your machine<br/>(not fenced)")]
```

Solid arrows are calls; dotted arrows are what comes back, or a callback.

## The parts

| Part | File | Its job | What it knows about |
| --- | --- | --- | --- |
| **Entry point** | `cli.py` | Parse flags, load config, build everything, start the REPL | Everything (the only file that does) |
| **Config** | `config.py` | Decide each setting: flag, then env var, then default | Nothing else |
| **System prompt** | `prompts.py` | Who the model is, how to work, plus live facts: folder, OS, date | Nothing else |
| **Agent** | `agent.py` | Own the conversation; run the agent loop; keep history valid and truthful | A `Provider`, a `ToolRegistry`, the message types |
| **Messages** | `messages.py` | Provider-neutral types; the history rules (`history_problems`) | Nothing else |
| **Provider** | `providers/` | Translate to a vendor's API and back; stream the reply | The vendor SDK, the message types |
| **Tool registry** | `tools/registry.py` | Look up tools, check arguments, get permission, run, turn failures into error results, cap output | The `Tool` interface, `Permissions` |
| **Tool base** | `tools/base.py` | What a tool is; turn an `Input` model into a schema and into checked arguments | Pydantic |
| **Permissions** | `tools/permissions.py` | Remember what's "always" allowed this session; otherwise ask, through a callback | Nothing else |
| **Tools** | `tools/*.py` | Do one job each; describe themselves to the model, and their effects to the user | The working directory (via `ToolContext`) |
| **REPL** | `repl.py` | Read your input, show events, ask permission questions, run slash commands | The `Agent`, the event and message types |

The rule behind this table: **each part knows as little as possible about the others.** The agent never prints and never touches files. Tools don't know there's a model, and never call `input()`. Nothing outside `providers/anthropic.py` knows what Anthropic's JSON looks like. That's what lets us swap the model, swap the interface, add tools, or test any part alone.

## Anatomy of a tool

```python
class EditFile(Tool):
    name = "edit_file"                       # ┐ what the model sees,
    description = "Change an existing…"      # ┤ together with the schema
    class Input(ToolInput): ...              # ┘ generated from this model

    def permission(self, args, ctx): ...     # what the user is asked (None = don't ask)
    def run(self, args, ctx): ...            # the work
```

One Pydantic `Input` model per tool is the single source for the JSON Schema sent to the model, the validation of what comes back, and the typed `args` object `run()` receives. `ToolInput` makes every input strict (no type conversions) and closed (unknown arguments are errors).

## The life of a turn

What happens when you type *"rename helper to assist in util.py"* and press Enter:

1. **`Repl.turn`** calls `agent.run_turn(text)` and shows each event it yields.
2. **`Agent.run_turn`** appends your message.
3. **The loop begins.** `_call_model` sends the system prompt, the **entire** history and the **tool specs** to the provider.
4. **The provider** translates to Anthropic's format and streams the reply. Text arrives as `TextDelta` events, which the REPL prints immediately. One `Completed` event ends the reply, carrying the assembled message, the stop reason and the token counts.
5. **The agent appends the reply** to the history. Does it contain tool requests?
   - **No:** the turn is over. The agent yields `TurnFinished`, and the REPL prints the usage line.
   - **Yes:** carry on to step 6.
6. **For each tool request,** the agent yields `ToolStarted`, calls `registry.run(name, args)`, and yields `ToolFinished`. The registry:
   1. finds the tool, or returns an error result listing the real ones;
   2. parses the arguments into the tool's `Input`, filling in defaults (`Tool.parse`);
   3. asks the tool what it's about to change (`Tool.permission`). Read-only tools say nothing. `edit_file` works out the edit and returns a diff; an edit that can't work fails here;
   4. if there's a request, asks `Permissions.allows`. That returns at once if the scope is already "always" allowed, and otherwise calls the REPL's `ask_permission`, which shows the request and reads y / n / a. A no becomes an error result telling the model not to retry;
   5. calls `tool.run(args, ctx)`, turning any exception into an error result;
   6. caps the output's size.
7. **All the results go into one user message**, appended to the history, and the loop returns to step 3.
8. **If the loop reaches `max_steps`,** the agent appends a short assistant note saying it stopped, and yields `TurnFinished(hit_step_limit=True)`.

**If the turn is cut short** between steps 2 and 8 (Ctrl-C, a network error, a bad API key), nothing is deleted. `Agent._close_unfinished_turn` ties off the loose ends: every unanswered tool request gets a result (the real one if the tool finished, otherwise a note that it was cut short or never ran), and a closing assistant note records where and why the turn stopped, after any text that had already streamed.

## Events and callbacks

The agent's output is a stream of events. The REPL decides how each looks:

| Event | From | Meaning | Shown as |
| --- | --- | --- | --- |
| `TextDelta` | provider | a fragment of the model's text | printed as it arrives |
| `Completed` | provider | one model call finished | (newline; warning if cut off) |
| `ToolStarted` | agent | about to run a tool | `▸ edit_file(path='util.py', …)` |
| `ToolFinished` | agent | the tool's result | `→ util.py +1 −1`, or `✗ declined` in red |
| `TurnFinished` | agent | the whole turn is over | `↳ 3,210 in · 250 out · 2 model calls` |

One thing flows the other way. A permission question needs an answer *before* the tool runs, so it's a **callback**, not an event: `cli.py` sets `permissions.ask = repl.ask_permission`. It's called synchronously, between `ToolStarted` and `ToolFinished`.

## Permission scopes

| Tool | Asks? | "Always" scope | Covers |
| --- | --- | --- | --- |
| `list_files`, `read_file`, `search` | never | | |
| `edit_file`, `write_file` | yes | `edit` | all file edits (working directory only, never `.git`) |
| `shell`, simple command | yes | `shell:<first word>` | commands starting with that word |
| `shell`, with any shell syntax (`;`, `&&`, pipes, `$`, redirects, brackets, line breaks) | every time | none | |

Scopes live in `Permissions.always`, in memory, until Wren exits. `/permissions` lists them.

## Rules that always hold

These are *invariants*: things the code guarantees, so the rest of the code can rely on them.

1. **Between turns, the history is valid.** `history_problems(agent.messages)` is empty: it starts with the user and ends with the assistant, every `tool_use` is answered by id in the next message, and no message is empty.
2. **The history is truthful.** Anything a tool did stays in the history, even if the turn was interrupted or failed. Nothing that happened is ever removed (except by `/clear`).
3. **Every provider stream ends with exactly one `Completed`.** Zero or more `TextDelta` come first.
4. **Tools never crash the loop.** Every failure becomes an error result the model can read. Only Ctrl-C escapes.
5. **Nothing changes without permission.** A tool whose `permission()` returns a request runs only if `Permissions.allows` said yes. With nobody to ask, the answer is no.
6. **Nobody is asked about a call that can't run.** Arguments are validated, and edits worked out, before the question.
7. **File tools stay inside the working directory.** Every path is resolved (following `..` and symlinks) *before* it's checked. Writing tools also refuse anything inside `.git`. *(The shell is the exception: it is not fenced. See the journal.)*
8. **No tool result exceeds 50,000 characters.**
9. **Vendor formats never leak.** Only a provider module sees its vendor's request and response shapes.
10. **The agent and the tools never do I/O with the user.** The agent yields events; permission questions go through a callback. Front ends decide what to show.

## Where the next steps plug in

| Coming in | Plugs in at |
| --- | --- |
| Step 4: richer UI | A replacement for `repl.py`, including `ask_permission`; the agent and tools don't change |
| Step 5: context management | Inside `Agent`, just before each call to the provider |
| Later: saved permission rules | Loaded into `Permissions.always` at startup |
| Later: sandboxing | Around `tools/shell.py`'s `_run` |
