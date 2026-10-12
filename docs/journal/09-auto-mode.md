# 09 · Auto mode, and a sandbox to make it safe

*Step 9 · October 2026*

## What we built

Type `/auto` and Wren stops asking about most things:

```text
/auto
Auto mode is on. Edits to files in this project, and commands in the sandbox (no network, writes
only inside the project), go ahead without asking. Wren still asks before a command outside the
sandbox (installing packages, git commit or push), and before edits in .envrc, .vscode, .idea,
.claude, .codex. /auto again turns it off.
```

The footer shows `auto: on` in yellow while it lasts. `/auto` again turns it off, and quitting forgets it, like "always" answers.

What makes that safe is the other half of the step: **on macOS, every shell command now runs in a sandbox** (`tools/sandbox.py`). The operating system, not Wren, stops it writing outside the project, reaching the network, or reading your keys. That applies whether auto mode is on or not.

The step came from you saying, of the permission prompts, *"I'd be lying if I said I was checking."*

---

## Idea 1: a question nobody reads protects nothing

Step 3 made every edit and command ask first (D23). That design has a failure mode, and you'd found it: when questions come every few seconds, you answer them without reading. Each `y` still *looks* like approval, so the safeguard is still there on paper, but it no longer protects anything.

This is common enough to have a name, **approval fatigue**, and every coding agent meets it. Adding more questions makes it worse. There are two ways out:

- **Ask fewer questions, so each one is worth reading.** Only ask where an answer matters.
- **Make the remaining risk something you don't have to watch.** Put a limit in place that holds whether anyone is reading or not.

Auto mode does the first. The sandbox does the second, and it's what makes the first reasonable.

## Idea 2: a judge or a fence

You asked whether Wren could use Claude Code's or Codex's auto-mode classifiers: a second model that looks at each action and decides whether it's safe. As far as we could tell, neither is offered as an API Wren could call. Building our own is possible, and is still an option for later (D100), but compare the two approaches:

| | A judge (a reviewer model) | A fence (a sandbox) |
| --- | --- | --- |
| Decides by | judgement | a rule the operating system enforces |
| Can be talked round | yes: it reads the same files the agent does | no |
| Costs | a model call per action | nothing per action |
| Gets wrong | subtle cases, either way | nothing inside its rules, but its rules are blunt |

A judge is flexible and fallible. A fence is rigid and can't be argued with. Codex CLI is built on the fence: commands run in a sandbox, and its approval settings decide when to ask about leaving it. You chose that route (option 1 of four, D100).

## Idea 3: "contained" is something Wren works out, never something the model says

The whole of auto mode is two lines at the top of `Permissions.allows`:

```python
def allows(self, request: PermissionRequest) -> bool:
    if self.auto and request.contained:
        return True
```

and one new field on the request each tool builds:

```python
# True when this can only change the project, and nothing in it that
# would run by itself later: an edit to an ordinary file, or a command
# in the sandbox. Auto mode allows these without asking. False, the
# default, means it's asked about in every mode.
contained: bool = False
```

What matters is **who sets `contained`**. The model never does. For an edit, Wren works it out from the path, which has already been resolved and fenced:

```python
contained=not runs_by_itself(ctx, plan.path),
```

For a command, it's whether the command will run in the sandbox:

```python
def _sandboxed(args: Shell.Input) -> bool:
    """Whether this call runs in the sandbox. permission() and run() both
    ask, so what you're asked about is what runs."""
    return sandbox.available() and not args.outside_sandbox
```

The model does get one choice, `outside_sandbox`, and it only goes one way: the model can ask to leave the fence, which always brings a question, but it can't ask to be trusted. A model that's been misled by text in a file can do no more in auto mode than the sandbox lets it.

The default is `False`, so a tool that doesn't think about containment, like a future MCP tool, is asked about in every mode. When you forget something, the result should be a question, not a hole.

## Idea 4: how a sandbox works on macOS

macOS has a sandbox built into its kernel, called **Seatbelt**. Apps from the App Store run in it. You describe what's allowed in a **profile**, written in SBPL (Sandbox Profile Language, a small Lisp), and the command-line program `sandbox-exec` applies a profile to a program:

```bash
sandbox-exec -p '(version 1) (allow default) (deny network*)' curl https://example.com
# curl: (6) Could not resolve host: example.com
```

Two properties make it useful:

- **It's inherited.** Every process the command starts gets the same limits, and nothing inside can remove them. `sh -c "python setup.py"` can't escape by starting Python.
- **It's enforced in the kernel.** The rule is checked when a program opens a file, not by parsing the command beforehand. This is why the sandbox doesn't care how clever the command line is. Step 3 couldn't parse shell syntax reliably (D28), and the sandbox doesn't need to.

Apple marks `sandbox-exec` as deprecated, but still ships it and uses it. Codex CLI and Claude Code both run commands this way on macOS, and Wren's profile was written after reading both of theirs.

`sandbox.command` builds the arguments, and the shell tool runs them in place of the command line:

```python
return [SANDBOX_EXEC, "-p", _profile(params), *flags, "/bin/sh", "-c", line]
```

## Idea 5: deny everything, then allow what's needed

The profile's first rule is `(deny default)`. Everything after it is a permission:

```lisp
; Reading: anywhere, except where credentials live.
(allow file-read*)
(deny file-read* (subpath (param "SECRET_0")) ...)

; Writing: the project and the temporary folders...
(allow file-write* (subpath (param "ROOT")) (subpath (param "TEMP_0")) ...)
```

The other way round, `(allow default)` and then a list of things denied, is much shorter, and it was the first draft. It has a hole you wouldn't guess. macOS programs often ask *system services* to do things for them, over a channel called Mach. One of those services launches apps. Under an allow-default profile, `open -a Terminal` works, and the Terminal it opens is **outside the sandbox**, because the system started it, not the command. So does an AppleScript telling another app what to do.

With deny-default, a command can talk only to the services the profile names: who the user is, logging, temporary folders, notifications, power settings. That list is the one Codex and Anthropic's sandbox-runtime use, cut down to what terminal programs need. Testing inside the sandbox: `osascript` asking for an app gets "Connection invalid", where outside it works.

**When two rules match, the later one wins.** That's what lets the profile allow the whole project and then take parts back:

```lisp
(allow file-write* (subpath (param "ROOT")) ...)
; ...but not what would run by itself later. This comes after the line
; above on purpose: when two rules match, the later one wins.
(deny file-write* (subpath (param "PROTECTED_0")) ...)
```

## Idea 6: the files that run themselves

"Writes only inside the project" sounds like enough, but some files in a project get *run*, later, by something other than Wren, and that run is outside the sandbox:

- `.git/hooks/pre-commit` runs the next time anyone commits, and `.git/config` can name programs for git to run.
- `.envrc` runs when you `cd` into the folder, if you use direnv.
- `.vscode/tasks.json` can run when the folder opens in an editor.
- `.claude/` and `.codex/` hold other agents' settings, including hooks.

A command that writes a git hook has escaped, with a delay. So these are `PROTECTED`:

```python
PROTECTED = (".git", ".envrc", ".vscode", ".idea", ".claude", ".codex")
```

A sandboxed command can't write them. An edit to one, through `edit_file` or `write_file`, isn't contained, so auto mode asks about it. (The writing tools already refuse `.git` entirely, D30.)

The two checks differ, on purpose. The file tools check every folder in the path, so `sub/.vscode/tasks.json` asks too. The sandbox only protects these names at the top of the project, because SBPL can match a path *relative to a parameter* only by its start. Matching "a `.vscode` folder anywhere inside" would mean writing the project's path into a regular expression, which Idea 7 rules out. That gap is listed below.

*(Later, D116: it didn't need pasting. SBPL's `string-append` can build the regular expression inside the profile from an escaped parameter, so the sandbox now checks every folder too. Worktrees in `.worktrees/` prompted it.)*

With `.git` read-only, `git status`, `git log` and `git diff` work in the sandbox, and `git add` and `git commit` don't: they write to `.git`. Committing is something to ask about anyway.

## Idea 7: never paste a path into a program

The profile needs the project's path, the temporary folders and your home directory. The obvious way is a format string: `f'(subpath "{root}")'`. That breaks, and worse, changes meaning, for a folder whose name contains a `"`. Then the folder's name becomes part of the profile's code.

It's the same mistake as building SQL by pasting in user input. And the fix is the same: pass the values separately. `sandbox-exec -D NAME=value` defines a parameter that the profile reads with `(param "NAME")`, so the profile's text is fixed and the paths are only ever data:

```python
flags = [f"-D{name}={value}" for name, value in params.items()]
```

One trap: Seatbelt matches **real** paths, after following symlinks. On macOS `/tmp` is a symlink to `/private/tmp`, so a rule for `/tmp` matches nothing. `_params` passes every path through `os.path.realpath` first.

## Idea 8: telling the model

The model has to know about the sandbox, or it will be confused when `curl` "can't resolve host". It's told in two places:

- **In the shell tool's description**, but only on a machine with a sandbox. The tool list is fixed for a conversation (invariant 16), and so is whether this machine has a sandbox, so the description doesn't change mid-conversation.
- **In the result, when a sandboxed command fails.** The sandbox's refusals look like ordinary errors ("Operation not permitted"), so the result says where the command ran and what to do if that's why it failed:

```text
[Exit code 1]
[This command ran in the sandbox: no network, and writes only inside the working directory and temp
folders, not .git, .envrc, .vscode, .idea, .claude, .codex. If that is why it failed, run it again
with outside_sandbox set to true. The user will be asked.]
```

Auto mode is different: it switches on and off mid-conversation, and the system prompt can't change mid-conversation (invariant 17). So the prompt and the edit tools' descriptions now say the user *may* be asked to approve something, which is true in both modes (D106).

## Idea 9: what changed when auto mode is off

Commands run in the sandbox in normal mode too (D101: proposed as a design call, and you confirmed it). The question tells you which:

```text
Run a shell command in the sandbox
Run a shell command outside the sandbox
```

"Always" keeps the two apart. Approving `uv` in the sandbox gives the scope `shell:uv`; outside it, `shell-outside:uv`. So "always allow `uv`" for `uv run pytest` doesn't quietly cover `uv pip install` with the network on. A side effect improves D28's worst case: "always allow `git`" in the sandbox can no longer push or commit.

---

## Python worth knowing from this step

- **`Popen` with a list, or with `shell=True`.** A string with `shell=True` hands the line to `/bin/sh` to parse. A list runs the program directly, with those exact arguments. The sandboxed form is a list whose last three items are `/bin/sh`, `-c` and the line, so the shell that parses the command is *inside* the sandbox. `_run` takes either: `shell=isinstance(command, str)`.
- **`os.path.realpath`** follows every symlink to the real path. `Path.resolve()` does the same; the module uses `realpath` because it needs strings for the flags.
- **`tempfile.gettempdir()`** on macOS isn't `/tmp`. It's a private folder per user, under `/var/folders/`. Tools use both, so both are writable.
- **A safe default on a dataclass field.** `contained: bool = False` means every existing caller of `PermissionRequest` got the cautious behaviour without changing a line.
- **`pytest.mark.skipif` as a named marker.** `sandbox_only = pytest.mark.skipif(not sandbox.available(), reason=...)` lets the tests that really run `sandbox-exec` skip cleanly on Linux, while the permission logic is tested everywhere.

## Known limits (on the list for later)

- **macOS only.** On Linux the same idea needs different machinery (bubblewrap, or the kernel's Landlock); on Windows, more again. Without a sandbox, auto mode still asks before every command, and says so.
- **Commands get your environment variables, API keys included.** With no network they can't send them anywhere, but a command that prints them (`env`) puts them in the conversation, which goes to the provider and into the session file. That was already true before this step; stripping keys from a sandboxed command's environment is a small follow-up.
- **Reading is broad.** A command can read anything you can, apart from the credential folders: other projects, shell history. Whatever it prints goes to the model.
- **Protected names are protected only at the top of the project inside the sandbox** (Idea 6). A command could write `sub/.envrc`. *(Fixed after this step: the sandbox now protects them in every folder, D116.)*
- **Memory files aren't protected.** In auto mode, Wren can edit `WREN.md` or `AGENTS.md` without asking, and the next conversation reads them. They can't grant permissions (D53), but they can steer.
- **The sandbox fences what runs now, not what's written for later.** Wren can write a test, a `Makefile` or a `package.json` script that runs outside the sandbox the next time *you* run it. Read changes before running them yourself.
- **The network is all or nothing.** Installing a package means leaving the sandbox entirely. Anthropic's sandbox-runtime allows a list of domains through a proxy; that's a bigger piece of work.
- **A sandbox can't start inside another.** If Wren itself runs inside a sandbox (started by another agent, say), every sandboxed command fails, and the model has to ask for `outside_sandbox`.
- **Not yet tried by hand in the full-screen app.** It's been tested end to end in the plain REPL. The app uses the same command and footer code, and the tests cover both.

## Things to try

1. `wren --provider fake`, then `run touch .git/x`. Read the question's title, approve it, and read the result.
2. `/auto`, then `write notes.txt hello` (no question) and `write .vscode/tasks.json {}` (a question).
3. `/permissions` with auto mode on, then with it off.
4. `run curl -sI https://example.com` and `run cat ~/.ssh/known_hosts`. Then the same commands in your own terminal.
5. In your own terminal: `sandbox-exec -p '(version 1) (allow default) (deny network*)' open -a Calculator`. Calculator opens: that's the hole from Idea 5. Then, in Wren, `run open -a Calculator`.
6. In `_profile`, move the `(deny file-write* ...PROTECTED...)` line above the `(allow file-write* ...)` line, and run step 1 again.
7. With a real model and auto mode on, ask it to commit its work. Does it ask for `outside_sandbox` straight away, or try in the sandbox first and read the note?

## What's next

Step 10 is extensions (MCP, subagents, hooks), moved along by one, as step 8 moved them before. Auto mode leaves follow-ups too: stripping API keys from sandboxed commands, a Linux sandbox, a reviewer model for commands that leave the sandbox (option 4 of the four), and a `--auto` flag to start in auto mode ([issue #9](https://github.com/bensullivan2002/wren/issues/9)).
