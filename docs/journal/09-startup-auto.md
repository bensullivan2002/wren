# 09 follow-up · Starting in auto mode

*Issue #9 · October 2026 · D136*

## What changed

`wren --auto` and `WREN_AUTO=1 wren` now start either front end in auto mode. The startup notice says ordinary project file edits won't ask, and reminds you that protected edits and commands outside the sandbox still ask. With no sandbox, it explicitly warns that every command still asks.

This doesn't broaden auto mode: since D117, sandboxed commands already run without questions in either mode. Auto mode adds ordinary file edits, not unrestricted commands.

## Why an absent flag isn't false

Configuration follows flag, then environment, then off. Python's `argparse` normally gives a `store_true` flag the default `False`. Passing that through would hide `WREN_AUTO=1` even when you didn't supply a flag. Instead:

```python
parser.add_argument(
    "--auto",
    action="store_true",
    default=None,
    help="start in auto mode: ordinary project file edits won't ask (env: WREN_AUTO)",
)
```

`None` means “not specified”; `True` means “chosen explicitly”. `Config.load` reads the environment only for `None`. It accepts explicit true and false spellings, rather than using `bool(value)`: `bool("false")` is **True**, because the string isn't empty. Invalid values produce a configuration error.

## One permission state, two front ends

The wiring stays in `cli.py`:

```python
permissions = Permissions()
permissions.auto = config.auto
```

The startup text uses the existing `notice` argument, alongside any resume result. Both front ends already display it, and neither adds it to the model's history. The footer reads the same permission state; no second auto-mode setting lives in the UI.

## Things to try

- `wren --provider fake --auto --plain`, then `write hello.txt hello`: no approval question. Try `write .envrc hello`: it still asks.
- Use `/auto`, then try another ordinary edit: questions return.
- Launch with `WREN_AUTO=1 wren`, then with `WREN_AUTO=0 wren`: the environment choice applies only at startup.
- On a system without a sandbox, check the warning and decline a command: auto mode must not bypass that question.

## Limits and what's next

Auto mode isn't saved in `settings.json` or restored from a conversation. A shell-profile `WREN_AUTO=1` does affect every launch, so use it deliberately; the startup notice makes that visible. There is no `--no-auto` flag: `WREN_AUTO=0 wren` overrides the environment for one launch.

The existing sandbox limits remain: macOS only, broad reads and inherited credentials. This follow-up doesn't change containment or “always allow” rules. Future sandbox work belongs at that boundary, not in flag parsing.
