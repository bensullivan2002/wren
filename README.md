# Wren

A small, readable coding-agent harness for the terminal, built step by step so every part can be understood.

> **Status: step 1 of the roadmap.** Wren chats with Claude in your terminal and streams replies as they're written. It can't read or change files yet; that's step 2.

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

**No API key?** `wren --provider fake` starts Wren with a pretend model that echoes you back. It's free and offline, which makes it handy for poking at the interface.

## Using it

Type a message and press Enter. Replies stream in as they're written, followed by a dim line with the tokens that turn used.

| Command    | What it does                                             |
| ---------- | -------------------------------------------------------- |
| `/help`    | List the commands                                        |
| `/history` | Show the conversation exactly as Wren holds it           |
| `/system`  | Show the system prompt sent with every request           |
| `/usage`   | Tokens used this session                                 |
| `/clear`   | Start a fresh conversation                               |
| `/quit`    | Exit (Ctrl-D works too)                                  |

Ctrl-C while Wren is replying stops the reply and discards that turn. Ctrl-C at the prompt clears the line.

## Configuration

Each setting can come from a flag, an environment variable, or the default, in that order of priority.

| Flag           | Environment variable | Default             |
| -------------- | -------------------- | ------------------- |
| `--provider`   | `WREN_PROVIDER`      | `anthropic`         |
| `--model`      | `WREN_MODEL`         | `claude-sonnet-5-5` |
| `--max-tokens` | `WREN_MAX_TOKENS`    | `8192`              |

## How it works

The docs are written to teach, not just to reference:

- **[Architecture](docs/ARCHITECTURE.md)**: how Wren is put together right now, and how a turn flows through it.
- **[Decisions](docs/DECISIONS.md)**: every significant choice we made, and why.
- **[Build journal](docs/journal/)**: one entry per step, explaining the ideas behind what we built.
  1. [Foundations: a chat loop you own](docs/journal/01-foundations.md)

## Roadmap

- [x] **1. Foundations**: conversation state, a swappable model provider, streaming, the terminal loop
- [ ] **2. Tools, read-only**: the real agent loop; Wren can look at files and search code
- [ ] **3. Acting safely**: editing files and running commands, behind permission prompts
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
  cli.py          entry point: flags -> config -> provider -> agent -> REPL
  config.py       settings and where they come from
  prompts.py      the system prompt, including live environment details
  agent.py        owns the conversation; runs one turn at a time
  messages.py     provider-neutral message and stream-event types
  repl.py         the terminal front end
  providers/
    base.py       the Provider interface
    anthropic.py  Claude, via the Anthropic Messages API
    fake.py       a pretend model for tests and offline use
tests/            unit and end-to-end tests
docs/             architecture, decisions, build journal
```
