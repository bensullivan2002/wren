# Wren

A small, readable coding-agent harness for the terminal, built step by step so every part can be understood.

> **Status: step 8 of the roadmap.** Wren is a full-screen terminal app that chats with Claude or OpenAI's models, explores your project, and changes it: it edits files and runs commands such as your tests, asking your permission first. It saves every conversation so you can resume it, switches models mid-conversation, reads standing instructions from `WREN.md` or `AGENTS.md`, caches what it re-sends, and compacts long conversations to keep them affordable. The shell is not sandboxed, so read what you approve.

## Quickstart

You need Python 3.10 or newer and an API key from Anthropic or OpenAI.

```bash
git clone https://github.com/bensullivan2002/wren.git
cd wren
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

export ANTHROPIC_API_KEY=sk-ant-...
wren
```

If you use [uv](https://docs.astral.sh/uv/): `uv sync --extra dev`, then `uv run wren`.

**No API key?** `wren --provider fake` starts Wren with a pretend model. It's free and offline. It echoes what you type, except for a few commands that make it call real tools, so you can watch the agent loop and the permission prompts work: `ls [pattern]`, `read <path>`, `search <regex>`, `write <path> <text>`, `edit <path> <old> => <new>`, `run <command>`.

## Using it

Run `wren` inside the project you want to work on: its file tools can only see that folder. It opens a full-screen app:

![Wren asking permission for an edit](docs/images/wren-tui.svg)

Type in the box at the bottom. Replies stream into the conversation as rendered markdown. Each tool call shows as one line; edits show their diff, and commands the end of their output.

| Key | What it does |
| --- | --- |
| Enter | Send your message |
| Ctrl+J | Start a new line in your message |
| Escape | Stop Wren while it's working |
| `y` / `a` / `n` | Answer a permission question |
| Ctrl+C | Copy the text you've selected with the mouse |
| Ctrl+P | Textual's command palette (themes, and more) |
| Ctrl+Q | Quit |

Escape stops Wren at once in most cases. Anything it had already done stays done, and stays in the conversation, so Wren knows where it left off.

**Plain mode.** `wren --plain` gives the original line-by-line interface, and Wren uses it automatically when its input or output isn't a terminal (`echo "explain main.py" | wren`). There, Ctrl-C stops a turn and Ctrl-D quits.

### Permission

Wren asks before every file edit and every shell command, and shows you exactly what it will do.

| Answer | Meaning |
| ------ | ------- |
| `y`    | Yes, this once |
| `n`    | No. Wren is told you declined, and should ask how you'd like to proceed |
| `a`    | Yes, and don't ask again this session for this kind of thing: all file edits, or commands starting with the same word |

`a` is only offered for simple commands: anything with `;`, `&&`, pipes, `$` or redirects is asked about every time. "Always" answers are forgotten when you quit. Reading, listing and searching never ask.

| Command         | What it does                                          |
| --------------- | ----------------------------------------------------- |
| `/help`         | List the commands                                     |
| `/tools`        | List the tools Wren can use                           |
| `/tools NAME`   | Show exactly what the model is told about one tool    |
| `/history`      | Show the conversation exactly as Wren holds it        |
| `/system`       | Show the system prompt sent with every request        |
| `/usage`        | Tokens used this session, and how full the context is |
| `/compact`      | Replace the conversation with a summary, to save tokens |
| `/model`        | Choose a different model (`/model NAME` switches directly) |
| `/effort`       | Choose how hard the model works (`/effort LEVEL`)     |
| `/resume`       | Go back to an earlier conversation in this folder     |
| `/permissions`  | Show what you've said Wren may always do this session |
| `/memory`       | Show which memory files are loaded                    |
| `/remember X`   | Add the note X to the project's memory file           |
| `/clear`        | Start a fresh conversation (the old one stays saved)  |
| `/quit`         | Exit                                                  |

### Saved conversations

Every conversation is saved as it happens, to `~/.wren/sessions/`, grouped by project folder.

| To | Do |
| --- | --- |
| Pick an earlier conversation as you start | `wren --resume` |
| Go straight to one | `wren --resume ID` (the first few characters will do) |
| Switch conversation from inside Wren | `/resume` |
| Start a new one | `/clear` (the old one is kept) |

Resuming brings back the conversation and the model it was using. It does not bring back "always allow" answers, and your files are however they are now, so Wren may need to re-read them. If Wren was closed mid-turn, the unfinished step is marked as such.

The files are plain text, readable only by your user account, and kept until you delete them. They contain whatever Wren read, so treat them as you would the project itself.

### Providers

| Provider | Start with | Key | Starts on |
| --- | --- | --- | --- |
| Claude (the default) | `wren` | `ANTHROPIC_API_KEY` | `claude-sonnet-5-5` |
| OpenAI | `wren --provider openai` | `OPENAI_API_KEY` | `gpt-5.6-terra` |
| A pretend model, offline | `wren --provider fake` | none | `demo` |

`export WREN_PROVIDER=openai` makes OpenAI the default. Everything else works the same on either. The OpenAI provider is tested against the SDK's types but has not yet been run against the live API. A conversation resumed under a different provider carries on, without the earlier model's private reasoning.

### Models

`/model` lists the models your provider offers and switches to the one you pick (it lists one provider's models: to change provider, restart with `--provider`); `/model NAME` switches directly. The conversation carries over. The first reply from a new model costs full price for the whole conversation, because the cache is kept per model. `/usage` shows the model the API reports as having written the last reply, so you can confirm a switch took effect. `--model` or `WREN_MODEL` sets the model Wren starts with.

`/effort` chooses how hard the model works on each reply (how much it thinks, how thorough it is): higher is slower and costs more. The levels offered are the ones the current model accepts (for OpenAI, every level the API knows, since it can't be asked per model); `/effort LEVEL` sets one directly, `/effort default` hands the choice back to the model, and `--effort` or `WREN_EFFORT` sets it at launch. Changing model puts effort back to that model's default. Some providers offer effort as part of the model choice and not as a separate setting; there, `/effort` tells you to look in `/model`.

`/usage` shows what the default effort is for the current model (from Anthropic's documentation: the API doesn't report it), the effort Wren sent with the last request, and how many thinking tokens the API reported for the last reply. The API never states which effort it used, so thinking tokens are the only measurable sign that a change took effect.

### Memory: standing instructions

Wren reads instructions from files at the start of every conversation and puts them in its system prompt:

| File | For |
| --- | --- |
| `~/.wren/WREN.md` | Your preferences, in every project |
| `WREN.md` in the project | How to build and test, conventions, what to leave alone |
| `AGENTS.md` in the project | The same, under the [open-standard](https://agents.md) name; used where a folder has no `WREN.md` |

Project files are looked for in the working directory and each folder above it, up to the top of the git repository. Where files disagree, the more specific one wins, and what you say in the conversation wins over all of them. Keep them short: they're sent with every request.

`/remember run the tests with make check` adds a line to the project's file. Changes take effect in the next conversation (`/clear`, or restart). Wren tells you at startup which files it loaded; `/memory` lists them.

**These files steer the model, and in a repository you've cloned, you didn't write them.** They can't approve anything: every edit and command still asks you first.

### Long conversations

Every request re-sends the whole conversation, so a long session costs more with each step. Wren does two things about it:

- **Caching.** The part of each request that was sent before is billed at about a tenth of the price. Nothing to configure. `/usage` shows how much was read from the cache.
- **Compaction.** When the conversation passes the context budget (150,000 tokens by default), Wren asks the model to summarise it and carries on from the summary. The status line shows how close you are (`context 12%`). Run `/compact` to do it at a moment you choose, such as after finishing a task. A summary loses detail: Wren may need to re-read files afterwards.

### Tools

| Tool         | What it does                                                    |
| ------------ | --------------------------------------------------------------- |
| `list_files` | List files and folders, optionally by glob (`**/*.py`)          |
| `read_file`  | Read a file with line numbers, 500 lines at a time              |
| `search`     | Find lines matching a regular expression across the project     |
| `edit_file`  | Replace an exact piece of text in a file (asks first)           |
| `write_file` | Create a file, or replace one completely (asks first)           |
| `shell`      | Run a command: tests, git, builds (asks first)                  |

The file tools are confined to the working directory, skip folders like `.git`, `node_modules` and `.venv` when listing and searching, and never write inside `.git`. **`shell` is not confined:** a command runs as you, with everything you can reach. The permission prompt is the safeguard.

## Configuration

Each setting can come from a flag, an environment variable, or the default, in that order of priority.

| Flag           | Environment variable | Default             |
| -------------- | -------------------- | ------------------- |
| `--provider`   | `WREN_PROVIDER`      | `anthropic` (or `openai`, `fake`) |
| `--model`      | `WREN_MODEL`         | the provider's default |
| `--max-tokens` | `WREN_MAX_TOKENS`    | `32000`             |
| `--max-steps`  | `WREN_MAX_STEPS`     | `25`                |
| `--context-budget` | `WREN_CONTEXT_BUDGET` | `150000`       |
| `--effort`     | `WREN_EFFORT`        | the model's default |
| `--resume [ID]` |                     |                     |
| `--plain`      |                      | off                 |

`--max-tokens` limits one reply, including the model's private thinking. `--max-steps` limits how many times Wren calls the model in one turn before it stops and checks in with you. `--context-budget` is how large the conversation may grow before Wren compacts it; 0 means never.

## How it works

The docs are written to teach, not just to reference:

- **[Architecture](docs/ARCHITECTURE.md)**: how Wren is put together right now, and how a turn flows through it.
- **[Decisions](docs/DECISIONS.md)**: every significant choice we made, and why.
- **[Build journal](docs/journal/)**: one entry per step, explaining the ideas behind what we built.
  1. [Foundations: a chat loop you own](docs/journal/01-foundations.md)
  2. [Tools: the agent loop](docs/journal/02-tools.md)
  3. [Acting safely: edits, commands and permission](docs/journal/03-acting-safely.md)
  4. [A full-screen interface](docs/journal/04-terminal-ui.md)
  5. [Context: what a long conversation costs](docs/journal/05-context.md)
  6. [Memory: instructions that outlast a conversation](docs/journal/06-memory.md)
  7. [Sessions, and switching models](docs/journal/07-sessions.md)
  8. [A second provider: OpenAI](docs/journal/08-openai.md)

## Roadmap

- [x] **1. Foundations**: conversation state, a swappable model provider, streaming, the terminal loop
- [x] **2. Tools, read-only**: the real agent loop; Wren can list, read and search files
- [x] **3. Acting safely**: typed tool inputs (Pydantic), editing files and running commands, behind permission prompts
- [x] **4. A better terminal UI**: a full-screen app with rendered markdown, diffs, in-place permission questions, and Escape to stop
- [x] **5. Context management**: prompt caching, a context meter, and compaction at a token budget
- [x] **6. Project memory**: standing instructions from `WREN.md` or `AGENTS.md`, per project and personal, with `/remember`
- [x] **7. Sessions**: conversations saved as they happen, a picker to resume them, and model switching
- [x] **8. A second provider**: OpenAI, through the same interface as Claude
- [ ] **9. Extensions**: subagents, MCP servers, hooks

## Development

```bash
pytest
```

The tests use a fake model, so they need no API key and no network. The app's tests run it headless and press its keys.

```
src/wren/
  cli.py            entry point: flags -> config -> provider + tools -> agent -> a front end
  config.py         settings and where they come from
  prompts.py        the system prompt, and the prompt used to compact a conversation
  memory.py         finds and loads WREN.md / AGENTS.md; /remember
  sessions.py       saves conversations as JSON Lines; lists and loads them
  agent.py          owns the conversation; runs the agent loop; compacts it
  messages.py       provider-neutral message types, and the history rules
  tui.py            the full-screen front end (Textual)
  repl.py           the plain front end (--plain, pipes)
  commands.py       slash commands, shared by both front ends
  providers/
    base.py         the Provider interface
    anthropic.py    Claude, via the Anthropic Messages API
    openai.py       OpenAI's models, via the Responses API
    fake.py         a pretend model for tests and the offline demo
  tools/
    base.py         the Tool interface; Pydantic inputs -> schema and checked arguments
    registry.py     looks up, checks, gets permission for, runs and caps every tool call
    permissions.py  what needs asking, and what's "always" allowed this session
    files.py        path fencing, folder walking, glob matching, safe text read/write, diffs
    list_files.py   ┐
    read_file.py    ├ read-only tools
    search.py       ┘
    edit_file.py    ┐
    write_file.py   ├ tools that change things (and ask first)
    shell.py        ┘
tests/              unit and end-to-end tests
docs/               architecture, decisions, build journal
```
