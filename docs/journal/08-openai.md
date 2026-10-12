# 08 · A second provider: OpenAI

*Step 8 · October 2026*

## What we built

Wren can now run on OpenAI's models as well as Claude's.

```bash
export OPENAI_API_KEY=sk-...
wren --provider openai                 # starts on gpt-5.6-terra
wren --provider openai --model gpt-6-astra --effort high
```

Everything works as before: tools, permission prompts, `/model`, `/effort`, `/compact`, saved sessions, memory files. You can make `openai` the default with `WREN_PROVIDER=openai`.

The step added one file, `providers/openai.py`, and a few lines of wiring. **Nothing in `agent.py`, `tools/`, `tui.py`, `repl.py`, `sessions.py` or `memory.py` changed.** That sentence is the result of this step. Step 1 claimed that keeping Wren's own message types would make the model swappable; this is the first time the claim was tested with a real second model.

> **Not yet run against the real API.** The provider is tested with a stand-in client that records requests and plays back responses built from the OpenAI SDK's own types. Your first session with a key is the real test.

---

## Idea 1: the same conversation, in a different shape

Both APIs carry the same things: what was said, tool requests, tool results, private reasoning. They arrange them differently.

| | Claude (Messages API) | OpenAI (Responses API) |
| --- | --- | --- |
| The conversation is | a list of **messages**, each with a role and a list of blocks | a flat list of **items**, of several kinds |
| A tool request | a `tool_use` block inside the assistant's message | a `function_call` item, on its own |
| A tool result | a `tool_result` block inside a *user* message | a `function_call_output` item, belonging to nobody |
| Reasoning | `thinking` blocks inside the assistant's message | `reasoning` items, before the items they led to |
| The system prompt | `system` | `instructions` |
| Tool arguments | a JSON object | a **string** containing JSON |
| Caching | asked for explicitly | automatic |

Step 2's journal remarked that a tool result goes in a *user* message because "as far as the API is concerned, there are only two parties". That turns out to be Claude's framing, not a law. OpenAI's list has no such rule: a result is just an item.

So the provider is two translations:

```python
def to_input_items(messages):                 # Wren -> OpenAI: flatten
    items = []
    for message in messages:
        for block in message.content:
            item = _block_to_item(block, message.role)
            if item is not None:
                items.append(item)
    return items
```

and `from_response`, which gathers one response's items back into a single assistant message. Each of Wren's four block types maps to exactly one kind of item, and order is kept both ways.

Wren's neutral types sit closer to Claude's shape, because Claude came first. It didn't matter. The agent's rules (every tool request answered, in the next message) are about *Wren's* history; each provider renders that history its own way.

## Idea 2: what the abstraction had to already have

A second provider fits only if the neutral layer already holds everything it needs. Three earlier decisions turned out to be load-bearing.

**The sealed envelope (step 5).** OpenAI's reasoning models, like Claude's, need their reasoning handed back between tool calls. It arrives encrypted. `ReasoningBlock(provider, data)` was built to carry exactly this without opening it:

```python
elif item.type == "reasoning":
    blocks.append(ReasoningBlock(OpenAIProvider.name, item.model_dump(mode="json", exclude_none=True)))
```

Each provider drops the other's envelopes, so a conversation that moves between providers stays valid; it just loses the private reasoning.

**Usage with a cache breakdown (step 5).** OpenAI reports input tokens as one total, cached ones included. Wren's `input_tokens` means the *uncached* part. So `_usage` subtracts, and `sent_tokens` adds back up to OpenAI's total. The meter, `/usage` and compaction then work without knowing which provider is underneath.

**Effort as a provider capability (step 7).** `effort`, `efforts()` and `default_effort()` were added to the interface for Claude with a second provider in mind. OpenAI's version is `reasoning: {"effort": ...}`.

## Idea 3: we keep the history, here too

OpenAI's API offers to remember the conversation for you: send an id for the previous response and only the new message. It's less to send, and it would make this provider unlike everything else in Wren, which assumes the history is `agent.messages`: compaction rewrites it, sessions log it, `/history` shows it, another provider can pick it up.

So every request says `store: False` and carries the whole conversation, as with Claude. That's why the reasoning has to travel with us, and why the request asks for it in encrypted form.

## Idea 4: a default that isn't neutral

```python
"strict": False,
```

This one line in `to_api_tools` is there because leaving it out does something. OpenAI's "strict" mode guarantees arguments match the schema, but requires every argument to be listed as required, with optional ones expressed as "may be null". If you don't say, the API tries to convert your schema to that form.

Wren's tools have real optional arguments with defaults (`limit: int = 500`), and step 3's strict Pydantic models reject `null` for them. A converted schema would have the model sending `"limit": null`, and Wren refusing its own tool calls. So the provider says no, explicitly.

The lesson generalises: **when an API has a default, find out what the default does before relying on it.** "I didn't set anything" is a setting.

## Idea 5: arguments as a string

OpenAI sends tool arguments as text that should be JSON. Usually it is. If a reply is cut off part-way, it isn't. `_arguments` doesn't crash and doesn't guess:

```python
try:
    parsed = json.loads(raw or "{}")
except json.JSONDecodeError:
    return {"invalid_json_arguments": raw[:200]}
```

It hands the tool an argument that step 3's validation will refuse (`unknown argument 'invalid_json_arguments'`), and the error goes back to the model with the text it sent. Step 2's rule again: failures are results the model can read.

## Idea 6: what each API will and won't tell you

Step 7 ended with a rule: say what was measured, say what's only believed, and label which is which. A second provider shows why it has to be per provider.

| Question | Claude's API | OpenAI's API |
| --- | --- | --- |
| Which model answered? | reported | reported |
| How many thinking tokens? | reported | reported (`reasoning_tokens`) |
| Which effort levels does this model take? | reported, per model | **not reported** |
| What's this model's default effort? | not reported | not reported |
| What effort was used for this reply? | not reported | not reported |

So for OpenAI, `/effort` offers every level the API knows (`none` to `max`), and one the model doesn't take is refused by the API on the next request. Defaults come from a dated table copied from OpenAI's guide, and a model the guide doesn't cover shows as unknown. `/usage` labels all of it.

One more wrinkle: listing OpenAI's models returns everything on the account, including image, speech and embedding models. The picker filters by name. It's a guess at "can this hold a conversation", and it's labelled as one in the code.

## Idea 7: a default per provider

`DEFAULT_MODEL` was one constant, a Claude model. With two providers it became a table, because a Claude model name means nothing to OpenAI:

```python
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-5.6-terra", "fake": "demo"}
```

The API-key check became a table the same way. A reliable sign that a second case has arrived: constants turn into dictionaries.

---

## Python worth knowing from this step

- **A module named like a library.** `wren/providers/openai.py` does `import openai` and gets the installed package, not itself, because Python 3's imports are absolute. (It would get itself only if run as a script from inside that folder.)
- **`json.dumps` and `json.loads` on arguments.** Wren holds tool input as a dict; OpenAI wants a string. Convert at the edge, in both directions, and nowhere else.
- **`getattr(obj, "name", None) or 0`**, used again for optional usage details.
- **`str.startswith` with a tuple:** `i.startswith(("gpt-", "o"))` tests several prefixes at once.
- **`SimpleNamespace`** in the tests: a quick object with whatever attributes you give it, for standing in for an SDK client.
- **Building test data from the SDK's own types.** The tests construct `Response.model_validate({...})`, so the SDK itself checks that the fake responses have a real response's shape. That caught a missing field while this was being written.

## Known limits (on the list for later)

- **Untested against the real API** (see the note at the top).
- **`/effort` can offer a level the model rejects,** because OpenAI's API can't be asked (Idea 6).
- **Moving a conversation between providers loses the private reasoning,** and a session saved under one provider keeps its model only when resumed under the same one.
- **Only OpenAI itself.** There is no setting for another base URL, and the many "OpenAI-compatible" services mostly speak the older Chat Completions API, which this provider doesn't.
- **Text only.** No images in or out.
- **The model filter is a heuristic** and may hide a usable model or show an unusable one. `/model NAME` works for any name.
- **Compaction and the system prompt were tuned on Claude.** They may want adjusting for other models.

## Things to try

1. With a key: `wren --provider openai`, ask something that needs a file read, then `/history`. Find the `<reasoning>` line.
2. Read `to_input_items` and `to_api_messages` (in `providers/anthropic.py`) side by side. Same input, two shapes.
3. `/usage` after a few messages: are cache reads showing? OpenAI caches by itself, so they should appear without Wren asking.
4. `/effort`, choose `none`, and send a message. Does this model accept it? Then try `xhigh`.
5. Start a session with Claude, quit, and `wren --provider openai --resume`. What does the resume note say, and does the conversation carry on?
6. In `to_api_tools`, delete the `"strict": False` line and (with a key) ask for a file read with no `limit`. What does the model send?
7. Run `git diff --stat` for this step's commit and see which files it touched.

## What's next: step 9, extensions

The roadmap's last step, moved along by one: **MCP** (tools provided by other programs), **subagents** (a task handed to a fresh conversation), and **hooks** (your own commands run at set moments). They are three separate pieces, and the first decision is which to build first.
