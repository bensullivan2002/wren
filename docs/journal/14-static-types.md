# Static type checking (issue #15)

Tests execute particular cases. A type checker reads the code without running it and asks whether the values passed around match the interfaces we promised. Both are useful: a test can check a real reply's behaviour, while a checker can spot a caller using an optional session store as though it always exists.

## Measure before choosing

basedpyright has six modes. We measured them against the unchanged source and tests before choosing standard mode for `src/` only (D135). Standard found 15 source diagnostics; strict found 250. Tests added 172 in standard and 3,102 in strict. Recommended also reports warnings, so comparing only its error count would hide most of its workload. D135 records every count and the measurement setup.

Fifteen findings were small enough to fix up front. There is no baseline file silently accepting old errors. Tests remain outside the type-checking scope: many inspect union events or replace SDK objects with stand-ins, which needs additional narrowing and annotations. That can be its own measured change later.

## An iterator doesn't promise close

The agent already closes a provider's stream on interruption:

```python
with closing(events):
    for event in events:
        ...
```

But `Iterator[StreamEvent]` only promises that `next()` works. It says nothing about `.close()`. Every current provider is a generator, so the return annotation now says what callers actually require:

```python
) -> Generator[StreamEvent, None, None]:
```

The three arguments are the yielded type, the type accepted by `.send()`, and the final return type. `Agent.run_turn` and `compact` get the same correction. Their implementation hasn't changed; the interface is now truthful. A future provider must honour closing too, not just yield events.

## Lists are invariant

A `list[ToolResultBlock]` isn't a `list[Block]`, even though every tool result is a block. Why? Someone holding a `list[Block]` could append a text block, breaking the original list's promise that it holds only tool results.

When results enter a message, the agent now builds a new list in the message's type context:

```python
self._add(Message("user", [*results]))
```

The new list can safely hold any block. The agent's separate results list keeps its narrower promise. Changing all message content to a read-only `Sequence` wasn't appropriate: other code legitimately appends blocks.

## Optional values and interfaces

`self.journal = None` alone let the checker infer that the journal could only be None. `self.journal: Journal | None = None` expresses the intended extension point: any object implementing the journal protocol, or no journal.

Other fixes make facts visible where they're used: a session path comes back from its creator, Textual's logo query is typed as `Static`, and the optional session store is checked before resuming. Assertions express existing budget and billing-route invariants. The callback handler uses a local `cast(_Server, self.server)` because only our `_Server` installs it; a cast documents that knowledge but performs no runtime validation. It doesn't narrow a mutable field on the base class for every caller.

## One configuration, one locked version

`pyproject.toml` specifies standard mode, `src/`, Python 3.10 and all platforms. `uv.lock` pins basedpyright and the Node runtime its Python distribution brings along. Run:

```bash
uv sync --locked --extra dev
uv run --locked --extra dev basedpyright
uv run --locked --extra dev pre-commit run --all-files
```

The hook needs uv on PATH. It checks the entire source tree rather than receiving staged filenames: a changed method signature can break an untouched caller. Source, dependency and configuration changes trigger it; docs-only commits don't. The dedicated CI job has a stable name, **Type check**, for branch protection. Making that name required is a separate GitHub setting, to finish after the first successful PR run.

## Things to try and limits

- Temporarily pass a string where an integer is expected in source, run basedpyright, then undo it. No model call or test is needed to find the mismatch.
- Temporarily change a method signature and see a finding in a different module. This is why the hook checks the whole source tree.
- Run the matching pytest tests after a fix. A clean type check cannot prove that a tool request is replayed correctly or that a UI behaves properly.
- `Any`, notably at SDK and JSON boundaries, remains an escape hatch. Standard mode doesn't make those values safe. Pydantic validation, defensive parsing and runtime tests still matter.
- The checker targets Python 3.10 syntax and APIs on all platforms, but runs on one CI host; the runtime tests still cover Linux and macOS and both supported endpoint Python versions.

Possible next steps are stricter checking in selected modules and type-checking tests, after measuring their cost again. Neither is silently enabled by this change.
