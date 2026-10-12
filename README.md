# Wren

A small, readable coding-agent harness for the terminal, built step by step so every part can be understood.

> **Status: step 4 of the roadmap.** Wren chats with Claude, explores your project, and changes it: it edits files and runs commands such as your tests, asking your permission first. Use it in its own plain terminal REPL, or through a richer interface such as [Toad](https://github.com/batrachianai/toad), which drives Wren over the Agent Client Protocol. The shell is not sandboxed, so read what you approve.

## Quickstart

You need Python 3.10 or newer and an Anthropic API key.

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

## Using it with Toad (ACP)

`wren acp` makes Wren speak the [Agent Client Protocol](https://agentclientprotocol.com) on stdin and stdout, so an ACP client can be its interface: rendered markdown, diffs, permission dialogs. [Toad](https://github.com/batrachianai/toad) is one such client, for macOS and Linux:

```bash
uv tool install -U batrachian-toad --python 3.14
cd ~/some/project
toad acp "/full/path/to/wren/.venv/bin/wren acp"
```

Toad starts Wren in the project folder and passes on your environment, so `ANTHROPIC_API_KEY` must be set in the shell you start Toad from. To try it with no key, add `--provider fake` inside the quotes. Flags such as `--model` go there too.

Wren's ACP mode is tested against the protocol, but has not yet been run under Toad itself. It does not support saved sessions, modes or images yet.

## Using the REPL

Run `wren` inside the project you want to work on: its file tools can only see that folder. Type a message and press Enter. Replies stream in as they're written. Tool calls appear as they happen, followed by a dim line with the tokens the turn used:

```
› the area function is wrong, fix it and run the tests
Let me look.
  ▸ read_file(path='shapes.py')  → 2 lines
  ▸ edit_file(path='shapes.py', old_string='w + h', new_string='w * h')
    Edit shapes.py
    │ @@ -1,2 +1,2 @@
    │  def area(w, h):
    │ -    return w + h
    │ +    return w * h
    Allow? [y]es / [n]o / [a]lways allow all file edits this session: y
  → shapes.py +1 −1
  ▸ shell(command='pytest -q')
    Run a shell command
    │ pytest -q
    Allow? [y]es / [n]o / [a]lways allow commands starting with `pytest` this session: a
  → 3 lines
Fixed: area multiplied nothing, it added (shapes.py:2). The tests pass.
↳ 9,480 in · 420 out · 4 model calls
```

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
| `/usage`        | Tokens used this session                              |
| `/permissions`  | Show what you've said Wren may always do this session |
| `/clear`        | Start a fresh conversation                            |
| `/quit`         | Exit (Ctrl-D works too)                               |

Ctrl-C while Wren is working stops it. Anything it had already done stays done, and stays in the conversation, so Wren knows where it left off. Ctrl-C at the prompt clears the line.

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

`wren [chat|acp] [flags]`: `chat`, the default, is the REPL; `acp` is for clients such as Toad.

Each setting can come from a flag, an environment variable, or the default, in that order of priority.

| Flag           | Environment variable | Default             |
| -------------- | -------------------- | ------------------- |
| `--provider`   | `WREN_PROVIDER`      | `anthropic`         |
| `--model`      | `WREN_MODEL`         | `claude-sonnet-5-5` |
| `--max-tokens` | `WREN_MAX_TOKENS`    | `8192`              |
| `--max-steps`  | `WREN_MAX_STEPS`     | `25`                |

`--max-tokens` limits one reply; `--max-steps` limits how many times Wren calls the model in one turn before it stops and checks in with you.

## How it works

The docs are written to teach, not just to reference:

- **[Architecture](docs/ARCHITECTURE.md)**: how Wren is put together right now, and how a turn flows through it.
- **[Decisions](docs/DECISIONS.md)**: every significant choice we made, and why.
- **[Build journal](docs/journal/)**: one entry per step, explaining the ideas behind what we built.
  1. [Foundations: a chat loop you own](docs/journal/01-foundations.md)
  2. [Tools: the agent loop](docs/journal/02-tools.md)
  3. [Acting safely: edits, commands and permission](docs/journal/03-acting-safely.md)
  4. [Borrowing a UI: Wren speaks ACP](docs/journal/04-acp.md)

## Roadmap

- [x] **1. Foundations**: conversation state, a swappable model provider, streaming, the terminal loop
- [x] **2. Tools, read-only**: the real agent loop; Wren can list, read and search files
- [x] **3. Acting safely**: typed tool inputs (Pydantic), editing files and running commands, behind permission prompts
- [x] **4. A better UI, borrowed**: Wren speaks the Agent Client Protocol, so Toad (or an editor) can be its interface
- [ ] **5. Context management**: token budgets, compaction, prompt caching
- [ ] **6. Project memory**: a `WREN.md` file of standing instructions per project
- [ ] **7. Sessions**: save, list and resume conversations
- [ ] **8. Extensions**: subagents, MCP servers, hooks

Unscheduled: a nicer built-in REPL (markdown, spinners); sandboxing the shell; saved permission rules; an async core.

## Development

```bash
pytest
```

The tests use a fake model, so they need no API key and no network.

```
src/wren/
  cli.py            entry point: flags -> config -> provider + tools -> agent -> a front end
  config.py         settings and where they come from
  prompts.py        the system prompt, including live environment details
  agent.py          owns the conversation; runs the agent loop
  messages.py       provider-neutral message types, and the history rules
  repl.py           front end for a person: the terminal REPL
  acp/              front end for a program: the Agent Client Protocol
    jsonrpc.py      the wire: JSON-RPC messages, one per line, both ways
    server.py       translates ACP requests, updates and permission to and from the agent
  providers/
    base.py         the Provider interface
    anthropic.py    Claude, via the Anthropic Messages API
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
