# 05 · Context: what a long conversation costs, and what to do about it

*Step 5 · October 2026*

## What we built

Nothing you can see changed much: a meter in the status line, a `/compact` command, a fuller `/usage`. What changed is what a long session costs, and whether it stays healthy.

1. **Prompt caching.** The part of each request that Wren has sent before now costs about a tenth as much.
2. **A context meter.** Wren knows how big the conversation is, and shows it: `context 12%`.
3. **Compaction.** When the conversation passes a budget, Wren asks the model to summarise it and carries on from the summary. You can also run `/compact` yourself.
4. **A fix.** The model's private reasoning is now kept and handed back, as the API expects. Wren had been throwing it away.

Try compaction for free, with a budget small enough to trip at once:

```bash
wren --provider fake --plain --context-budget 100
> hello
> again                 # compacts first, then answers
> /history              # two messages where there were four
> /usage
```

---

## Idea 1: the bill grows with the square of the conversation

From step 1: the API has no memory, so **every request re-sends everything**. Follow one file through a session. Wren reads a 2,000-token file at step 3 of a turn. That turn runs 20 more model calls. The file is sent, and paid for, 20 more times: 40,000 tokens for one read.

Now add it up for a whole session. If each step adds about the same amount, request *n* carries *n* steps' worth, and the total is 1 + 2 + 3 + … + n. Twice as long a session costs about four times as much. That curve, not the model's limit, is the problem this step addresses. Current models accept a million tokens; you'd run out of patience with the bill long before you ran out of room.

There are two tools for it. They do different things, and you want both:

| | What it does | What it costs you |
| --- | --- | --- |
| **Caching** | Makes re-sending the same text cheap | Nothing: the model sees exactly the same conversation |
| **Compaction** | Makes the conversation smaller | Detail: the model forgets what the summary left out |

## Idea 2: caching is a prefix match

The API can remember a request it has just processed. If your next request *begins with exactly the same bytes*, that beginning is read back from the cache at about a tenth of the input price. In Wren it's one field in `providers/anthropic.py`:

```python
request["cache_control"] = {"type": "ephemeral"}
```

That asks the API to cache up to the end of this request and to move the marker forward as the conversation grows. The rest is not code. It's a property the code already had:

**A cache matches from the start, byte for byte.** Change one character near the front and everything after it is uncached. So caching works exactly when requests are built the same way every time, with new material only at the end. Wren's requests are: tools, then the system prompt, then the messages in order. An agent that stuffed the current time into its system prompt, or sorted its tools differently per request, would pay full price forever and never know why.

This is why step 3's history rule ("nothing that happened is ever removed") turns out to be an economic rule too. **An append-only history is a cacheable history.**

Three details worth knowing:

- The cache lasts **five minutes**, renewed each time it's read. An agent loop makes calls seconds apart, so it stays warm. Go for coffee and the next message pays full price once.
- Writing to the cache costs a little more than normal input (about 1.25×). It pays for itself on the second request.
- The system prompt contains today's date, which changes the prefix once a day. With a five-minute cache, that costs nothing, so it stays.

`Usage` now carries the split, because the provider reports it:

```python
input_tokens        # sent and processed afresh       (full price)
cache_write_tokens  # processed afresh AND saved      (a little more)
cache_read_tokens   # recognised from a recent call   (about a tenth)
```

Before this step `input_tokens` was the whole story. With caching on, it's only the uncached part, so the old usage line would have shown a long conversation costing almost nothing. `sent_tokens` adds the three.

## Idea 3: don't count tokens; read the meter

To know when the conversation is too big, we need its size in tokens. The obvious approach is to count: estimate from characters (wrong, especially for code), or call the API's token-counting endpoint before each request (accurate, but an extra round trip).

Wren does neither. Every reply already says how many tokens its request contained:

```python
self.context_tokens = completed.usage.sent_tokens + completed.usage.output_tokens
```

That's an exact count, by the only tokenizer that matters, for free. The request's input plus the reply it produced is what the next request will carry. The number is one call behind, which is fine for deciding when to tidy up.

The general lesson: **prefer measuring to predicting**, when the system will tell you the answer anyway.

## Idea 4: compaction is asking the model to write its own handover note

When the conversation passes the budget, `Agent.compact` makes one extra model call: the whole conversation, plus a request to summarise it. Then it swaps the history for two messages:

```
[1] user
    [The earlier part of this conversation was removed to save space. The
    assistant's own summary of it follows. Files may have changed since: read
    them again before relying on details.]
[2] assistant
    ## What the user wants
    …
```

Everything after that costs a fraction of what it did.

It is **lossy, and there's no way round that**. The model can no longer see the files it read or exactly what you said. What survives is decided by one piece of text: `COMPACT_PROMPT` in `prompts.py`. Read it. It asks for what a colleague taking over would need: what you want, what was done (and what you declined), what was learned the hard way, the current state, the next steps. It tells the model plainly that the summary is all it will have. Whatever the prompt doesn't ask for is forgotten, which makes it the most consequential prompt in Wren after the system prompt.

The alternative we didn't take is Anthropic's server-side compaction: a beta feature where the API does the summarising and returns an opaque block. Less code, but Claude-only, and you can't read or steer what was kept. Ours is one method and a prompt you can edit.

## Idea 5: do the dangerous step last

Compaction replaces your entire conversation. If it went wrong half-way, you'd lose everything. So `compact` is built so that it can't go wrong half-way:

```python
try:
    ...                                  # the model call; check we got a real summary
except Exception as e:
    yield Compacted(before, before, error=str(e) or type(e).__name__)
    return                               # nothing has been touched

self.messages[:] = [Message.user(COMPACTED_NOTE), Message.assistant(summary)]   # the swap
```

Everything that can fail (the network, a model that calls a tool instead of summarising, a summary cut off by `max_tokens`, you pressing Escape) happens *before* the one line that changes anything. Either compaction completes, or the history is exactly as it was. Tests cover each failure.

This shape, **prepare everything, then commit in one step**, is how databases and file saves avoid half-finished states too.

And when *automatic* compaction fails, the turn carries on uncompacted. A tidying-up step shouldn't be able to stop you working.

## Idea 6: when to compact

- **Automatically, between turns,** once the conversation is over the budget (150,000 tokens by default; `--context-budget`, or 0 for never). Between turns is the one moment the history is whole and nothing is in flight.
- **When you say so,** with `/compact`. The best moment is one you choose: after finishing a task, before starting an unrelated one. A summary of finished work loses little.

A single long turn can run past the budget before the next check. That's acceptable because the budget is a cost target, not the model's limit, which is several times higher.

The summarising call re-sends the same system prompt and tools, with the request on the end. Because that beginning matches the previous request, the whole conversation is read **from the cache**: summarising 150,000 tokens costs about what 15,000 would. Idea 2 paying for Idea 4.

### Follow-up: compacting near the model's window

The fixed 150,000 suited a 200,000-token model. Today's models hold a million tokens or more, so Wren was summarising (and losing detail) when only 15% of the window was used. Now the budget is worked out from the model's own window (D99):

```python
window - max_tokens - int(window * COMPACTION_MARGIN)   # 1,050,000 - 32,000 - 105,000 = 913,000
```

Each part has a job. `max_tokens` leaves room for the model's next reply. The 10% margin is for the case above: a single long turn that grows the conversation before the next check. `--context-budget` is now a cap that can only make compaction earlier, for example to keep requests cheaper.

The interesting part is where the window comes from, because the two providers answer differently:

| Question | Anthropic | OpenAI |
| --- | --- | --- |
| How big is this model's window? | reported (`max_input_tokens`) | **not reported**: copied from its model pages, with the date |

The same pattern as effort levels in step 8: ask where the API can answer, use a dated table where it can't, and say "unknown" rather than guess. An unknown window means no automatic compaction, and the footer says so instead of drawing a bar against a made-up number. `/usage` shows the window with its source:

```text
Context window: 1,050,000 tokens  (per OpenAI's model pages, …, checked 12 October 2026)
Compacts at: 913,000 tokens: the window, less 32,000 for a reply and 105,000 (10%) for a long turn to grow into
```

Asking Anthropic is a network call, and the footer redraws ten times a second, so the footer must never make it. The agent asks once per model and keeps the answer; the app asks from a worker thread, and the turn asks again if that failed. The trade-off is cost: a conversation can now grow much larger before it's summarised, and every request re-sends all of it.

## Idea 7: the sealed envelope (a bug fix)

Current Claude models think before they answer, and it can't be switched off. The API returns that thinking as blocks alongside the text, often with the content empty or encrypted, and expects them **sent back unchanged** on the next request. The model's later steps build on that reasoning, and the API may reject a conversation whose reasoning has been stripped or altered.

Since step 1, Wren dropped any block it didn't recognise. That was fine when it was written and wrong by the time these models were the default. Nothing had run against the real API yet, so it hadn't bitten.

The fix is a fourth block type that Wren carries but never opens:

```python
@dataclass
class ReasoningBlock:
    provider: str            # which provider produced it; others won't understand it
    data: dict[str, Any]     # the provider's own representation, returned exactly as it came
```

It's the one vendor-shaped thing in the history, and the rule is strict: only the provider that made it looks inside. The Anthropic provider stores each thinking block whole and sends it back in the same position; another provider's reasoning is left out.

This is also why compaction replaces *everything* and doesn't keep the last few messages verbatim, which would otherwise be a nice touch. Each reasoning block is tied to the conversation that came before it. Keep a recent message but swap what preceded it for a summary, and its reasoning no longer matches its history. A clean break has no such problem: the summary is plain text, and the conversation starts fresh from it.

A related change: the default `max_tokens` went from 8,192 to 32,000. Thinking counts against that limit, so the old value could cut a reply off before it reached the answer. It's a ceiling, not a cost.

---

## Python worth knowing from this step

- **`@property`.** `usage.sent_tokens` looks like a field but is computed on each access, so it can never disagree with the three numbers it adds.
- **Slice assignment.** `self.messages[:] = [...]` replaces a list's *contents* while keeping the same list object, so anything else holding a reference to it sees the change.
- **`x if x is not None else default`** versus **`x or default`.** `or` treats 0 as "not given". For `--context-budget 0` ("never compact"), 0 is the point. See `Config.load`.
- **`getattr(obj, "name", None) or 0`.** Reads a field that may be missing *or* None, and gets a number either way.
- **`model_dump(mode="json")`.** Turns a Pydantic object (here, the SDK's thinking block) back into plain dicts and strings.

## Known limits (on the list for later)

- **Not yet run against the real API.** Caching and reasoning blocks are tested with a stand-in for the SDK's client. The first real session is the real test.
- **Compaction only happens between turns.** One enormous turn isn't interrupted to compact.
- **Everything is summarised.** No recent messages are kept word for word (Idea 7 explains why).
- **A budget smaller than the fixed overhead** (the system prompt and tool descriptions, about 1,500 tokens) compacts on every turn and can never get under it.
- **Old tool results aren't trimmed.** Dropping stale file reads without summarising would be a cheaper, gentler step before compaction.
- **The five-minute cache expires while you think.** A longer cache lifetime exists, at a higher write price; Wren doesn't use it.
- **There's no cost in money,** only tokens. Prices differ by model and change.

## Things to try

1. Run the four commands at the top, then read the summary in `/history`.
2. Read `COMPACT_PROMPT`. What would you add for the way *you* work? Change it and compact again.
3. With a real key: have a short conversation and run `/usage`. How much of the input was read from the cache? Wait six minutes, send another message, and look again.
4. With a real key: `/history` after a tool call. Find the `<reasoning: …>` line and note where it sits in the message.
5. In `providers/anthropic.py`, imagine adding `f"Time: {datetime.now()}"` to the front of the system prompt. What happens to caching, and how would you find out?
6. Read the compaction tests in `tests/test_agent.py`: each failure case is one line of setup.

## What's next: step 6, project memory

Compaction forgets, and every new session starts from nothing. Step 6 gives Wren something that lasts: a `WREN.md` file per project, holding standing instructions and facts ("run tests with `make check`", "never touch `legacy/`"), loaded into every conversation. It raises good questions: where the file goes in the prompt so it doesn't break caching, whether Wren may write to it, and how it relates to the summary a compaction leaves behind.
