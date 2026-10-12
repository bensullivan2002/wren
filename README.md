# Wren

A small, readable coding-agent harness for the terminal, built step by step so every part can be understood.

> **Status: step 2 of the roadmap.** Wren chats with Claude in your terminal and explores your project with read-only tools: it lists, reads and searches files to answer from the actual code. It can't change anything yet; that's step 3.

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

**No API key?** `wren --provider fake` starts Wren with a pretend model. It's free and offline. It echoes what you type, except for three commands that make it call real tools, so you can watch the agent loop work: `ls [pattern]`, `read <path>`, `search <regex>`.

## Using it

Start Wren inside the project you want to ask about: its tools can only see that folder. Type a message and press Enter. Replies stream in as they're written. Tool calls appear as they happen, followed by a dim line with the tokens the turn used:

```
› where is the step limit enforced?
Let me find it.
  ▸ search(pattern='max_steps')  → 9 matches in 4 files
  ▸ read_file(path='src/wren/agent.py', offset=95, limit=40)  → lines 95-134 of 161
It's in Agent._loop (src/wren/agent.py:104)…
↳ 6,120 in · 310 out · 3 model calls
```

| Command         | What it does                                          |
| --------------- | ----------------------------------------------------- |
| `/help`         | List the commands                                     |
| `/tools`        | List the tools Wren can use                           |
| `/tools NAME`   | Show exactly what the model is told about one tool    |
| `/history`      | Show the conversation exactly as Wren holds it        |
| `/system`       | Show the system prompt sent with every request        |
| `/usage`        | Tokens used this session                              |
| `/clear`        | Start a fresh conversation                            |
| `/quit`         | Exit (Ctrl-D works too)                               |

Ctrl-C while Wren is working stops it and discards that turn. Ctrl-C at the prompt clears the line.

### Tools

| Tool         | What it does                                                    |
| ------------ | --------------------------------------------------------------- |
| `list_files` | List files and folders, optionally by glob (`**/*.py`)          |
| `read_file`  | Read a file with line numbers, 500 lines at a time              |
| `search`     | Find lines matching a regular expression across the project     |

All three are read-only, confined to the working directory, and skip folders like `.git`, `node_modules` and `.venv`.

## Configuration

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

## Roadmap

- [x] **1. Foundations**: conversation state, a swappable model provider, streaming, the terminal loop
- [x] **2. Tools, read-only**: the real agent loop; Wren can list, read and search files
- [ ] **3. Acting safely**: typed tool inputs (Pydantic), editing files and running commands, behind permission prompts
- [ ] **4. A better terminal UI**: rendered markdown, spinners, visible tool calls
- [ ] **5. Context management**: token budgets, compaction, prompt caching
- [ ] **6. Project memory**: a `WREN.md` file of standing instructions per project
- [ ] **7. Sessions**: save, list and resume conversations
- [ ] **8. Extensions**: subagents, MCP servers, hooks

## Development

```bash
pytest
```

The tests use a fake model, so they need no API key and no network.

```
src/wren/
  cli.py            entry point: flags -> config -> provider + tools -> agent -> REPL
  config.py         settings and where they come from
  prompts.py        the system prompt, including live environment details
  agent.py          owns the conversation; runs the agent loop
  messages.py       provider-neutral message types, and the history rules
  repl.py           the terminal front end
  providers/
    base.py         the Provider interface
    anthropic.py    Claude, via the Anthropic Messages API
    fake.py         a pretend model for tests and the offline demo
  tools/
    base.py         the Tool interface, ToolResult, ToolError
    registry.py     looks up, validates, runs and caps every tool call
    validation.py   a tiny JSON Schema checker (replaced by Pydantic in step 3)
    files.py        path fencing, folder walking, glob matching
    list_files.py   ┐
    read_file.py    ├ the tools themselves
    search.py       ┘
tests/              unit and end-to-end tests
docs/               architecture, decisions, build journal
```
