# 01 · Foundations: a chat loop you own

*Step 1 · October 2026*

## What we built

A terminal program that chats with Claude. You type, the reply streams in word by word, and a dim line tells you how many tokens the turn used. It can't touch your files yet; it knows that, and says so.

That sounds like very little. But almost every idea an agent harness depends on is already here, in small form. This entry walks through them, one at a time.

Try it first if you can:

```bash
wren                    # with ANTHROPIC_API_KEY set
wren --provider fake    # no key needed: a pretend model that echoes you
```

Then try `/history` and `/system`. They show you exactly what Wren sends to the model, which is the whole subject of this entry.

---

## Idea 1: the model is a function; the harness is everything else

Strip away the product and a language model is a function: it takes text and produces more text. It has:

- **no memory** between calls,
- **no hands**: it can't open a file or run a command,
- **no senses**: it doesn't know your OS, your folder, or today's date.

Everything that makes a coding agent feel capable comes from the program wrapped around the model. That program is the **harness**. Over this project, Wren's harness will grow to:

| The model can't… | So the harness… | Step |
| --- | --- | --- |
| remember | keeps the conversation and resends it | 1 ✅ |
| see its surroundings | describes them in the system prompt | 1 ✅ |
| act | runs tools on its behalf and reports back | 2 and 3 |
| be trusted blindly | asks you before risky actions | 3 |
| hold unlimited text | manages what fits in its context window | 5 |

## Idea 2: the API is stateless, so the conversation is just a list

Here's the most surprising fact for most people: **Claude does not remember your conversation.** Each API request stands alone. The only reason it seems to remember is that the harness sends the *entire* conversation every single time.

On your second turn, Wren's request looks like this:

```json
{
  "model": "claude-sonnet-5-5",
  "max_tokens": 8192,
  "system": "You are Wren, a coding assistant running in the user's terminal. …",
  "messages": [
    {"role": "user",      "content": [{"type": "text", "text": "What's a monad?"}]},
    {"role": "assistant", "content": [{"type": "text", "text": "A monad is a pattern for…"}]},
    {"role": "user",      "content": [{"type": "text", "text": "Shorter, please"}]}
  ]
}
```

The model reads all of it, then writes what comes next. So in Wren, the conversation's entire "memory" is one Python list, `Agent.messages`:

```python
self.messages.append(Message.user(user_text))     # before calling the model
...
self.messages.append(completed.message)           # after the reply arrives
```

Two consequences you'll feel later:

- **Cost grows every turn.** Watch the `↳ … in` number after each reply: it climbs, because each request includes everything before it. A long conversation re-sends thousands of tokens per message. (Step 5 deals with this, with caching and summarising.)
- **You can edit the past.** Since the harness holds the list, it can trim, rewrite or summarise it. The model will believe whatever history it's handed. That's power, and responsibility.

`/history` prints this list. `/clear` empties it, and the model genuinely forgets.

## Idea 3: a message is a list of blocks, not a string

Look at `content` in the JSON above: it's a *list* of typed blocks, not a plain string. In step 1 every block is `{"type": "text"}`, so this looks like overkill. It isn't. In step 2, one assistant message will hold text *and* a request to run a tool:

```json
{"role": "assistant", "content": [
  {"type": "text", "text": "Let me look at that file."},
  {"type": "tool_use", "id": "toolu_01", "name": "read_file", "input": {"path": "app.py"}}
]}
```

…and the next user message will carry the tool's result, not something you typed. So `messages.py` defines all three block types now (`TextBlock`, `ToolUseBlock`, `ToolResultBlock`). The shape of your core data is the hardest thing to change later, because everything touches it. Getting it right first is cheap; fixing it later is not.

## Idea 4: streaming, and a chain of generators

Without streaming, you'd stare at a blank screen until the whole reply was finished, possibly 30 seconds for a long answer. With streaming, the API sends the reply as a series of small events over one open connection (a format called *server-sent events*), and Wren prints each piece the moment it lands.

Wren passes these pieces along using Python **generators**: functions that use `yield` to hand back one value at a time, pausing in between. There are three of them, stacked:

```
AnthropicProvider.stream()   yields TextDelta("Hel"), TextDelta("lo"), …, Completed(...)
        ↓
Agent.run_turn()             passes each event up, and acts on Completed
        ↓
Repl.turn()                  prints each TextDelta, then the usage line
```

Nothing runs until the REPL asks for the next event, and each layer handles only what it cares about. The provider knows HTTP. The agent knows the conversation. The REPL knows the screen.

The contract between them is small and strict: **any number of `TextDelta`, then exactly one `Completed`, always last.** `Completed` carries the fully assembled message, the *stop reason* (why the model stopped: it finished, it hit the length limit, or, from step 2, it wants a tool), and the token counts.

One small detail with a big effect: the REPL prints with `flush=True`. Python normally buffers output and writes it in batches; without the flush, the words would arrive in bursts instead of a smooth stream.

## Idea 5: the provider boundary

Every model vendor has its own JSON format. If Anthropic's format spread through Wren, adding another provider would mean touching every file. So Wren draws a line:

- Inside Wren: only Wren's own types from `messages.py`.
- At the edge: a **provider** translates to the vendor's format on the way out, and back on the way in.

The interface every provider implements is tiny (`providers/base.py`): one attribute for its name, one for the model, and one method, `stream()`. It's a `Protocol`, which means a class counts as a Provider just by having that shape. No inheritance needed.

The payoff is already visible: **`FakeProvider`**. It's a pretend model with the same shape, which either echoes you or plays back a script. The agent can't tell it apart from Claude. That's why the tests run with no network and no key, and why `wren --provider fake` works offline. When we add OpenAI or a local model later, it'll be one new file in `providers/`.

## Idea 6: the system prompt tells the model where it is

The **system prompt** is standing instructions, sent separately from the conversation with every request. Wren's has two parts (`prompts.py`):

1. **Identity:** who it is and how to behave, including an honest statement of what it *can't* do yet. Without that, a model asked to "check my config file" might cheerfully pretend it had.
2. **Environment:** live facts the harness fills in: working directory, operating system, today's date.

Ask Wren what today's date is. It knows, but only because `build_system_prompt()` told it. Run `/system` to see the whole thing.

This pattern, *the harness feeds the model facts about the world*, is one we'll use constantly. In step 6, a project's own `WREN.md` file will be stitched in here too.

## Idea 7: keep the history valid, no matter what

What should happen if you press Ctrl-C halfway through a reply? Or the network drops?

Wren's answer: **the turn never happened.** `Agent.run_turn` remembers how long the history was before it started, and if anything goes wrong it deletes everything after that point:

```python
checkpoint = len(self.messages)
self.messages.append(Message.user(user_text))
try:
    ...                                   # stream the reply, append it
except BaseException:
    del self.messages[checkpoint:]        # undo this whole turn
    raise
```

Why so strict? A history with a question and no answer is confusing: the model would see it next time, and might answer both. Once tools arrive, it gets worse: an assistant's tool request with no matching result is *invalid*, and the API rejects the whole conversation. Keeping a simple rule now ("between turns, the history is always complete") saves a whole class of bugs later.

Two Python details make this work:

- `except BaseException`, not `except Exception`. Ctrl-C raises `KeyboardInterrupt`, which deliberately *isn't* an `Exception`, so ordinary error handlers don't swallow it.
- `with closing(events):` makes sure an abandoned stream gets closed, which closes the provider's stream, which closes the HTTP connection. Closing ripples down the whole chain of generators from Idea 4.

## Idea 8: tokens are the currency

Every request is billed in **tokens**: chunks of text, roughly four characters of English each. Input tokens are everything you send (system prompt and whole history); output tokens are what the model writes. Output tokens cost several times more than input tokens.

Tokens are also the model's **memory limit**: there's a maximum it can read in one request (its *context window*). Wren shows usage after every turn, and `/usage` gives the session total, so you build a feel for both early. `--max-tokens` caps how long one reply can be; if a reply hits that cap, Wren tells you it was cut off.

---

## Python worth knowing from this step

- **Dataclasses** (`@dataclass`): classes that are mostly data. Python writes `__init__`, equality and a readable printout for you. All of `messages.py` is dataclasses.
- **Generators** (`yield`): functions that produce values one at a time and pause in between. The backbone of streaming.
- **Context managers** (`with`): guaranteed clean-up, even when errors happen. Used for the HTTP stream and for closing generators.
- **`Protocol`**: "anything with this shape counts". Lets providers be interchangeable without a class hierarchy.

## Things to try

1. Chat for a few turns and watch the `in` number grow. Then `/clear`, ask the same question, and compare.
2. Tell Wren your name, then `/clear`, then ask what your name is.
3. Start a long reply and press Ctrl-C. Run `/history`: the turn is gone.
4. Run `wren --max-tokens 30` and ask for an essay.
5. Read `tests/test_agent.py`. Each test name is a sentence about how the agent behaves.

## What's next: step 2, tools

Step 2 turns this chat loop into an **agent loop**. We'll describe some tools to the model (read a file, list a folder, search code). When the model wants one, it stops with the reason `tool_use`. Wren runs the tool, appends the result to the history, and calls the model again, round and round, until the model says it's done.

We'll start with **read-only** tools, so Wren can explore your code but not change it. Changing things, and asking your permission first, is step 3.
