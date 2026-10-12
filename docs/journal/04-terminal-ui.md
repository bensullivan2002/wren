# 04 · A full-screen interface

*Step 4 · October 2026*

## What we built

Run `wren` and it now takes over the terminal, the way `vim` or `htop` does:

![Wren asking permission for an edit](../images/wren-tui.svg)

- The conversation scrolls in the middle. Replies are **rendered markdown**, streamed as they're written.
- You type in a box at the bottom. **Enter** sends; **Ctrl+J** adds a line.
- Tool calls appear as one line each. Edits show their **diff**; commands show the **end of their output**.
- Permission questions appear in the conversation, where they come up: **y**, **a** or **n**.
- Slash commands open in a panel above the prompt, and **Escape** closes it.
- **Escape** stops Wren. **Ctrl+Q** quits.

It's built on [Textual](https://textual.textualize.io), a Python framework for terminal apps. The old line-by-line interface is still there as `wren --plain`, and Wren switches to it by itself when it isn't attached to a terminal (`echo hi | wren`).

Try it with no API key:

```bash
mkdir /tmp/play && cd /tmp/play && wren --provider fake
```

Then type `write hello.py print("hi")`, `edit hello.py hi => hello`, `run python3 hello.py`, `/help`.

**`agent.py` did not change in this step.** That sentence is the point of the entry. Everything below is about what a second front end needs, and why the first three steps had already provided it.

---

## How we got here

The route is worth recording, because each turn in it taught something:

1. The first recommendation was an inline UI (Rich + prompt_toolkit): output scrolling up your normal terminal.
2. You asked about [Toad](https://github.com/batrachianai/toad), a finished Textual app that can front any agent speaking the Agent Client Protocol (ACP). We built that: `wren acp`, a hand-written JSON-RPC server. It worked in tests.
3. Using it felt wrong: you'd left Wren and were inside someone else's program. Wren was a back end.
4. So we removed the ACP mode and built Wren's own Textual app.

The ACP code is gone from the tree (it's in git history, commit `f2f8e5e`). Two things from it survived, because they turned out to be what *any* richer front end needs. They're Ideas 7 and 8 below.

## Idea 1: a different kind of program

The REPL is a straight line:

```python
line = input(PROMPT)        # wait for you
self.turn(line)             # then work; nothing else can happen meanwhile
```

That's why it can't show a spinner, or let you scroll while the model writes. While it's working, it isn't listening.

A Textual app is an **event loop**: one loop that never stops, which redraws the screen, notices keys and mouse, and in between runs small pieces of our code called **handlers**. You don't call the framework; it calls you:

```python
@on(Prompt.Submitted)
async def submitted(self, message):     # "the user pressed Enter"
    ...
```

The contract is that every handler finishes quickly. One that takes ten seconds freezes everything for ten seconds: no redraw, no scrolling, no Escape.

## Idea 2: the screen is a tree of widgets

Everything visible is a **widget**: an object that draws itself. `compose()` says which ones exist, top to bottom:

```python
def compose(self) -> ComposeResult:
    yield Static(..., id="header")
    yield VerticalScroll(id="transcript")     # the conversation
    yield Static("", id="status")
    yield Prompt(id="prompt", ...)            # where you type
```

As the conversation goes on, we **mount** more widgets inside the transcript: a `Static` for your message, a `Markdown` for the reply, a `ToolCallView` for each tool call, a `PermissionPrompt` for a question.

How they look is **CSS**, as on a web page:

```css
#prompt { height: auto; max-height: 10; border: round $accent; }
.permission { height: auto; margin: 1 0 0 2; padding: 0 1; border: round $warning; }
```

`$accent` and `$warning` come from the theme, so the app follows light and dark terminals without us choosing colours.

## Idea 3: the agent is slow, so it gets its own thread

The agent blocks, by design: on the network, on a shell command. It can't run inside a handler. So a turn runs on a second thread, which Textual calls a **worker**:

```python
@work(thread=True, exclusive=True, group="turn")
def run_turn(self, text: str) -> None:
    with closing(self.agent.run_turn(text)) as turn:
        for event in turn:
            if self._stop.is_set():
                break
            self._on_app_thread(self.show_event, event)
```

Two threads need a rule, and it's the one every UI toolkit has: **only the app's own thread touches the screen.** The worker never changes a widget. It asks the app to, with `call_from_thread`, and waits until that's done.

```
worker thread                         app thread (the event loop)
-------------                         ---------------------------
for event in agent.run_turn(text):
    call_from_thread(show_event)  ->  mount / update widgets
```

The waiting matters. It keeps events in order, and it's **backpressure**: a fast model can't queue up updates faster than the screen can draw them.

The alternative was to rewrite the agent, providers and tools as `async`, which is how most production agents are built. It was more than this step needed, and the thread keeps the core readable. It's logged as D34, open to revisiting.

## Idea 4: a permission question crosses the threads both ways

The tools layer calls `ask_permission(request)` and expects an answer back. But it's calling on the worker thread, and the answer is a key press that arrives on the app thread, some time later. The bridge is a **Future**: a box for one value that doesn't exist yet.

```python
def ask_permission(self, request):                       # worker thread
    answer = concurrent.futures.Future()
    self._on_app_thread(self.show_question, request, answer)
    while True:
        try:
            return answer.result(timeout=0.1)            # sleep until it's filled
        except concurrent.futures.TimeoutError:
            if self._stop.is_set():                      # Escape: stop, change nothing
                ...
                return Decision.DENY
```

```python
def action_answer(self, choice):                         # app thread, on a key press
    self.answer.set_result(Decision(choice))             # fills the box; the worker wakes
```

Compare this with `Repl.ask_permission`, which is `input()` in a loop. Same callback, same `PermissionRequest`, same `Decision`. The tools can't tell which one they're talking to.

## Idea 5: focus decides what a key means

How is `y` an answer at a question, and a letter in the prompt box otherwise? A key press goes to the widget that has the **focus**. `PermissionPrompt` is focusable and takes the focus when it appears:

```python
class PermissionPrompt(Widget, can_focus=True):
    BINDINGS = [
        Binding("y", "answer('allow')", "Yes"),
        Binding("a", "answer('always')", "Always"),
        Binding("n", "answer('deny')", "No"),
    ]
```

When it's answered, it hands the focus back to the prompt and removes itself. There's a test that checks `app.focused` at each stage, because getting this wrong would mean typing "y" into a message.

Escape is different: it's bound on the app with `priority=True`, so it works wherever the focus is.

### A bug that focus caused, found later

In real use, every permission question made the conversation jump to its very top, leaving the question waiting off-screen. The cause was one line: `question.focus()`. Focusing a widget scrolls it into view, and the widget had only just been mounted, so it hadn't been laid out yet and Textual took its position to be the top. That scroll also cancelled the "stay at the bottom" anchor, so the view stayed wrong afterwards.

The fix (`WrenApp.ask`) is to take the focus without scrolling, `widget.focus(scroll_visible=False)`, then pin the conversation to its end again, once immediately and once after the layout has caught up. The headless tests had missed it because they used conversations too short to scroll. There's now a test with a long one that checks the scroll position. **A UI test that never scrolls can't catch a scrolling bug.**

### The command panel: what isn't conversation doesn't go in the conversation

At first, the output of `/usage` or `/history` was added to the transcript like everything else. It piled up between the real messages, and a long `/history` pushed the conversation off the screen.

Those commands are you asking Wren about itself. Their answers aren't part of what's being said to the model. So they now open in a **panel**: a box above the prompt that shows one thing, and closes when you press Escape, leaving nothing behind. Pickers (`/provider`, `/model`, `/effort`, `/resume`) open in the same place.

```python
class CommandPanel(VerticalScroll):
    async def show(self, title, content, keys="↑↓ scroll"):
        await self.remove_children()
        await self.mount(content)
        self.border_title = title
        self.border_subtitle = f"{keys} · esc to go back"
        self.add_class("open")
        (content if content.can_focus else self).focus()
```

The panel is always in the layout, hidden by CSS (`display: none`), and "opening" it adds a class. That's the usual way to show and hide things in Textual, as on the web: change a class, and let the stylesheet do the rest.

What decides where a command's output goes is a flag on its result, set in `commands.py`:

| The command | Its output goes | Because |
| --- | --- | --- |
| tells you something (`/usage`, `/help`, `/history`, `/tools`, `/system`, `/memory`, `/permissions`) | in the panel | it's reference: read it and dismiss it |
| changes something (`/clear`, `/remember`, `/model NAME`, `/compact`) | in the transcript, as a note | it's an event in the session, and belongs where it happened |
| fails | in the transcript | so you can't miss it |

Escape now has two jobs, and they're ordered: with the panel open it closes the panel; otherwise it stops Wren. One press never does both, so looking something up mid-turn can't cancel the turn by accident. A permission question closes the panel by itself, because it needs the keyboard more than the panel does.

The plain REPL has no panel. The same commands print their text, which is all a line-by-line interface can do. The flag is a hint a front end may ignore.

## Idea 6: stopping is still closing the generator

Escape sets a flag (`threading.Event`). The worker sees it at the next event and leaves the loop. Leaving the `with closing(...)` block closes the agent's generator, which raises `GeneratorExit` inside the agent, which runs step 3's `_close_unfinished_turn`.

So Escape in the app tidies the history exactly as Ctrl-C does in the REPL. We built that mechanism for one front end, and the second got it without a line changing.

This is **cooperative** stopping: it happens when the worker next looks. That's instant while text streams. It isn't while the worker is blocked in a network call waiting for the model's first words.

## Idea 7: a way to stop a slow tool

A shell command can run for minutes, and the worker is blocked inside it, not looking at any flag. So `ToolContext` gained a hook:

```python
should_stop: Callable[[], bool] = lambda: False
```

By default it always says no. The app points it at its flag (`agent.tools.context.should_stop = self._stop.is_set`), and the shell now checks it five times a second while it waits:

```python
output, _ = process.communicate(timeout=POLL_SECONDS)
...
stopped = "cancelled" if should_stop() else "timeout" if time.monotonic() >= deadline else None
```

There's a test that starts `sleep 30`, presses Escape, and expects the turn to end at once.

## Idea 8: a change, as data

The REPL shows an edit as a text diff, which is all it can do. A richer interface wants the two versions, to draw the change its own way. So edits now carry a `FileChange(path, before, after)` on both the `PermissionRequest` and the `ToolResult`. The REPL ignores it; the app draws it (`change_text`).

This and `should_stop` are the only changes to the tools layer. The test for whether something belongs there: would it make sense with a different front end? Both first appeared for ACP, and both were needed again for Textual, which is good evidence.

## Idea 9: streaming markdown without redrawing everything

Markdown can't be rendered a word at a time: `**bo` isn't bold yet. The simple approach is to re-render the whole reply on every fragment, which gets slower as the reply grows. Textual has a helper that appends to what's already rendered and batches fragments that arrive faster than it can draw:

```python
self._stream = Markdown.get_stream(self._reply)
await self._stream.write(event.text)
```

## Idea 10: text from outside is never markup

Textual reads `[bold]` in a plain string as an instruction to itself. A model's reply, a file's contents or a command's output can contain anything, including `[/]`, which would crash the renderer or restyle the screen. So everything from outside goes in as a Rich `Text` object or with `markup=False`.

It's the same idea as escaping HTML, or the prompt injection from step 3: **data must never be read as instructions.** There's a test that types markup-looking text and reads a file full of it.

## Idea 11: two front ends, one set of commands

`/tools` should say the same thing in both interfaces. So the slash commands moved out of `repl.py` into `commands.py`, where each returns plain text and a couple of flags (`quit`, `cleared`). Each front end decides how to show it. Like the agent, that module never prints.

## Idea 12: testing a UI by pressing its keys

Textual can run an app **headless**: no terminal, but the real event loop, widgets and key handling. The tests type, press Enter, answer questions and press Escape:

```python
async with app.run_test() as pilot:
    await say(app, pilot, "edit a.txt one => 1")
    await until(pilot, lambda: question(app), "the permission question")
    assert app.focused is question(app)
    await pilot.press("y")
    await finished(app, pilot)
    assert (tmp_path / "a.txt").read_text() == "1\ntwo\n"
```

Note `until(...)`. The turn is on another thread, so a test can't assume how long anything takes. It waits for a condition, with a timeout. Sleeping for a fixed time is how UI tests become flaky.

---

## Python worth knowing from this step

- **`async def` and `await`.** A handler marked `async` can pause at an `await` and let the event loop do other things. `await self.transcript.mount(widget)` means "carry on when it's on screen".
- **Decorators that register.** `@on(Prompt.Submitted)` and `@work(thread=True)` don't change what the function does; they tell Textual when or how to run it.
- **`threading.Event`.** A flag one thread sets and another reads, safely.
- **`concurrent.futures.Future`.** A box for a value that arrives later, from another thread. `result(timeout=…)` waits for it.
- **Messages between widgets.** `Prompt` posts a `Submitted` message; the app handles it. The widget doesn't know who's listening, so it can be reused.
- **Importing late.** `cli.py` imports `wren.tui` inside the branch that uses it, so `--plain` never loads Textual.
- **`pytest-asyncio`.** Lets a test be `async def`. `asyncio_mode = "auto"` in `pyproject.toml` switches it on for all of them.

## Known limits (on the list for later)

- **Stopping can lag** while Wren waits for the model's first words (Idea 6).
- **No scrollback after you quit.** A full-screen app gives the terminal back as it found it. The conversation isn't saved until step 7.
- **Copying** is select-with-the-mouse then Ctrl+C, and depends on your terminal. In some you hold Shift (or Option on macOS) to select.
- **Lightly tested in real terminals.** The UI tests are headless, plus one scripted launch in a pseudo-terminal. Different terminals and themes may show rough edges.
- **You can't type the next message into a queue.** Sending while Wren works is refused; the text stays in the box.
- **Nothing shows while the model writes a tool request,** beyond the spinner. Showing it as it's written needs a new provider event.
- **Diffs and new files are cut at 40 lines,** command output at its last 10, with no way to expand them yet.
- **The plain REPL didn't improve.** No markdown or spinner there.

## Things to try

1. In `wren --provider fake`: `edit` a file and look at the question, then answer `a`, then edit again.
2. `run sleep 60`, answer `y`, press Escape. Then `/history`: what does the tool result say?
3. Type `[bold]hello[/]` as a message. Then remove `markup=False` from the `.user` Static in `tui.py` and try again.
4. Put `time.sleep(5)` at the top of `WrenApp.submitted` and send a message. Try to scroll. That's what "never block the event loop" means.
5. Press Ctrl+P in the app for Textual's command palette, and change the theme.
6. Run `echo "ls" | wren --provider fake`. Why did you get the plain interface?
7. Read `Repl.show` and `WrenApp.show_event` side by side.

## What's next: step 5, context management

Every request re-sends the whole conversation, and tool results make it grow fast. Step 5 is about that: counting tokens, a budget, compacting old history, and prompt caching so the unchanged prefix is cheap. It plugs in inside `Agent`, just before each call to the provider, and the status line gets a context meter.
