"""The footer: the rows at the bottom of the app that say what Wren is set
to, what it has spent, and what it's doing.

    model: claude-sonnet-5-5 · effort: high · provider: anthropic │ git: main · uncommitted: 2 │ ~/code/wren
    context: ▰▰▰▱▱▱▱▱▱▱ 28% · 42k/150k │ cache hit: 91% · compactions: 0 │ memory files: 1 · always allowed: 1
    cost: £2.71 · burn: £0.12/min │ in: 52k · out: 48k · cache read: 1.1M · cache write: 61k · thinking: 12k
    session: 23:10 · turns: 7 │ this turn: 0:42 · tokens/turn: 38k (avg 31k) │ first word: 1.8s
    tool calls: 24 · read_file 12 · shell 5 · search 4 · edit_file 3 │ files changed: 3 · tool errors: 1
    ⠹ step 4 · running edit_file(path='tui.py', …) · esc to stop

This module builds those rows; tui.py only puts them on screen. Kept apart
so they can be tested without running the app, and so tui.py stays about
the app itself.

Two ideas in it worth knowing:

  * A row is never wrapped. Each part of it has a `keep` score, and when
    the terminal is too narrow the most important parts are kept and the
    least important left out (`fit`). Wrapping would push the other rows
    around; this way every row stays where you expect it.

  * The git branch comes from running `git status` ourselves. That's run on
    a worker thread (see WrenApp.refresh_git), since in a big repository it
    can take long enough to freeze the screen.

Every piece of text from outside (a model name, a branch, a path, a tool's
arguments) goes into a Rich `Text`, so none of it is read as markup.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from rich.text import Text

from wren import __version__
from wren.agent import Agent
from wren.commands import budget_used, format_call, no_budget_reason, shorten
from wren.stats import RunStats

FOOTER_ROWS = 6
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
KEY_HINTS = "enter send · ctrl+j new line · /help · ctrl+q quit"
BAR_CELLS = 10


@dataclass
class GitState:
    branch: str  # or "detached at 1a2b3c4"
    uncommitted: int  # files with changes not yet committed, new files included


@dataclass
class AppState:
    """What the footer needs from the app, beyond the agent and the stats."""

    busy: bool = False
    stopping: bool = False
    frame: int = 0  # which spinner character to show
    git: GitState | None = None  # None: not a git repository, or git isn't installed
    memory_files: int | None = None  # None: memory files are switched off
    session_id: str | None = None  # None: nothing saved yet
    resumed: bool = False
    gbp_per_usd: Decimal | None = None  # None: no exchange rate yet, so money is shown in dollars


@dataclass
class Part:
    text: Text
    keep: int = 5  # 1 to 9: when a row doesn't fit, the lowest go first


# --- The rows -----------------------------------------------------------------------


def footer_rows(agent: Agent, stats: RunStats, state: AppState, width: int) -> list[Text]:
    rows = [
        _settings(agent, stats, state),
        _context(agent, stats, state),
        _spending(stats, state.gbp_per_usd),
        _time(stats),
        _tools(stats),
        _activity(agent, stats, state),
    ]
    return [fit(groups, width) for groups in rows]


def _settings(agent: Agent, stats: RunStats, state: AppState) -> list[list[Part]]:
    provider = agent.provider
    where = str(agent.tools.context.root).replace(str(Path.home()), "~", 1)
    groups = [
        [
            Part(kv("model", provider.model, "bold"), 9),
            Part(kv("effort", provider.effort or "default", "bold"), 9),
            Part(kv("provider", provider.name), 7),
        ]
    ]
    if agent.tools.permissions.auto:
        # Yellow, like "always allowed": edits and commands are going ahead without a question.
        groups[0].append(Part(kv("auto", "on", "yellow"), 8))
    other = stats.answered_by_another(provider)
    if other:
        groups.append([Part(Text(f"⚠ last reply came from {other}", style="yellow"), 8)])
    if state.git is not None:
        git = [Part(kv("git", state.git.branch), 6)]
        if state.git.uncommitted:
            git.append(Part(kv("uncommitted", str(state.git.uncommitted)), 3))
        groups.append(git)
    groups.append([Part(Text(where), 5)])
    if state.session_id:
        groups.append([Part(kv("session id", state.session_id + (" (resumed)" if state.resumed else "")), 2)])
    groups.append([Part(Text(f"wren {__version__} · {len(agent.tools)} tools", style="dim"), 1)])
    return groups


def _context(agent: Agent, stats: RunStats, state: AppState) -> list[list[Part]]:
    budget = agent.context_budget  # read once: it's worked out from the model's window
    if budget:
        meter = Text.assemble(("context: ", "dim"), bar(agent.context_tokens / budget), " ")
        meter.append(f"{budget_used(agent, budget)} · {tokens(agent.context_tokens)}/{tokens(budget)}")
    else:
        meter = kv("context", f"{tokens(agent.context_tokens)} ({no_budget_reason(agent)})")
    hit = stats.cache_hit()
    allowed = len(agent.tools.permissions.always)
    memory = "off" if state.memory_files is None else str(state.memory_files)
    return [
        [Part(meter, 9)],
        [
            Part(kv("cache hit", "—" if hit is None else f"{hit:.0%}"), 6),
            Part(kv("compactions", str(stats.compactions)), 3),
        ],
        [
            Part(kv("memory files", memory), 4),
            # Yellow when it's not zero: some edits or commands are going ahead without a question.
            Part(kv("always allowed", str(allowed), "yellow" if allowed else ""), 5),
        ],
    ]


def _spending(stats: RunStats, gbp_per_usd: Decimal | None) -> list[list[Part]]:
    u = stats.usage
    unknown = "+?" if stats.unpriced_calls else ""  # some calls aren't in the total
    if stats.unpriced_calls and not stats.cost:
        cost = ("$" if gbp_per_usd is None else "£") + "?"
    else:
        cost = money(stats.cost, gbp_per_usd) + unknown
    rate = stats.cost_per_minute()
    burn = "—" if rate is None else f"{money(rate, gbp_per_usd)}/min{unknown}"
    return [
        [Part(kv("cost", cost, "bold"), 9), Part(kv("burn", burn), 8)],
        [
            Part(kv("in", tokens(u.input_tokens)), 8),
            Part(kv("out", tokens(u.output_tokens)), 8),
            Part(kv("cache read", tokens(u.cache_read_tokens)), 6),
            Part(kv("cache write", tokens(u.cache_write_tokens)), 5),
            Part(kv("thinking", tokens(u.thinking_tokens)), 3),
        ],
    ]


def _time(stats: RunStats) -> list[list[Part]]:
    running = stats.turn_elapsed()
    if running is not None:
        turn = kv("this turn", clock(running))
    else:
        turn = kv("last turn", "—" if stats.last_turn_seconds is None else clock(stats.last_turn_seconds))
    average = stats.average_turn_tokens()
    if stats.last_turn_tokens is None:
        per_turn = "—"
    else:
        per_turn = f"{tokens(stats.last_turn_tokens)} (avg {tokens(average or 0)})"
    first = stats.first_word_seconds
    return [
        [Part(kv("session", clock(stats.elapsed())), 9), Part(kv("turns", str(stats.turns)), 8)],
        [Part(turn, 8), Part(kv("tokens/turn", per_turn), 6)],
        [Part(kv("first word", "—" if first is None else f"{first:.1f}s"), 4)],
    ]


def _tools(stats: RunStats) -> list[list[Part]]:
    calls = [Part(kv("tool calls", str(sum(stats.tool_calls.values()))), 8)]
    # Most used first; when space is short, the least used go first.
    for rank, (name, count) in enumerate(stats.tool_calls.most_common()):
        calls.append(Part(Text(f"{name} {count}"), max(2, 7 - rank)))
    errors = stats.tool_errors
    return [
        calls,
        [
            Part(kv("files changed", str(len(stats.files_changed))), 6),
            Part(kv("tool errors", str(errors), "red" if errors else ""), 6),
        ],
    ]


def _activity(agent: Agent, stats: RunStats, state: AppState) -> list[list[Part]]:
    if not state.busy:
        return [[Part(Text(KEY_HINTS, style="dim"), 9)]]
    spinner = Text(SPINNER[state.frame % len(SPINNER)] + " ", style="cyan")
    if state.stopping:
        return [[Part(spinner + Text("stopping…"), 9)]]
    if not stats.in_turn:  # /compact, or asking the API for a list of models
        return [[Part(spinner + Text("working"), 9), Part(Text("esc to stop", style="dim"), 8)]]
    if stats.running_tool is not None:
        doing = Text("running " + shorten(format_call(stats.running_tool), 60))
    else:
        doing = Text("waiting for the model")
    step = f"step {stats.step}/{agent.max_steps}" if agent.max_steps else f"step {stats.step}"  # no limit: no "/n"
    return [
        [
            Part(spinner + Text(step), 9),
            Part(doing, 7),
            Part(Text("esc to stop", style="dim"), 8),
        ]
    ]


# --- Laying out a row -------------------------------------------------------------


SEPARATOR_CELLS = 3  # " · " between parts and " │ " between groups are both three cells wide


def fit(groups: list[list[Part]], width: int) -> Text:
    """Join the parts into one line no wider than `width`: parts with " · "
    between them, groups of parts with " │ ".

    If they don't all fit, choose by `keep`: take the part with the highest
    first, then the next, skipping any that would make the row too wide,
    so the room left by a long part that didn't fit can still go to short
    ones that do. Among equals, the leftmost is taken first. The chosen
    parts stay in their places. If even the most important part is too
    wide on its own, it's cut short with "…".
    """
    places = [(g, p) for g, group in enumerate(groups) for p in range(len(group))]
    if not places:
        return Text()
    by_importance = sorted(places, key=lambda place: (-groups[place[0]][place[1]].keep, place))
    chosen: set[tuple[int, int]] = set()
    used = -SEPARATOR_CELLS  # the first part has no separator before it
    for g, p in by_importance:
        needed = SEPARATOR_CELLS + groups[g][p].text.cell_len
        if used + needed <= width:
            chosen.add((g, p))
            used += needed
    if not chosen:
        chosen.add(by_importance[0])
    kept = [[part for p, part in enumerate(group) if (g, p) in chosen] for g, group in enumerate(groups)]
    row = _join([group for group in kept if group])
    row.truncate(max(width, 0), overflow="ellipsis")
    return row


def _join(groups: list[list[Part]]) -> Text:
    group_texts = [Text(" · ", style="dim").join(part.text for part in group) for group in groups]
    return Text(" │ ", style="dim").join(group_texts)


# --- Formatting ----------------------------------------------------------------


def kv(key: str, value: str, style: str = "") -> Text:
    """"key: value", the key dimmed so the values stand out."""
    return Text.assemble((f"{key}: ", "dim"), (value, style))


def bar(fraction: float) -> Text:
    """▰▰▰▱▱▱▱▱▱▱: green, then yellow from 60%, red from 85%."""
    filled = min(BAR_CELLS, max(0, round(fraction * BAR_CELLS)))
    colour = "green" if fraction < 0.6 else "yellow" if fraction < 0.85 else "red"
    return Text.assemble(("▰" * filled, colour), ("▱" * (BAR_CELLS - filled), "dim"))


def tokens(n: int) -> str:
    """A token count, short: 980, 1.2k, 52k, 1.1M."""
    if n < 1_000:
        return str(n)
    # The limits are where rounding would carry into the next form: 9,960
    # to one decimal is "10.0k", which should be "10k"; 999,600 would be "1000k".
    if n < 9_950:
        return f"{n / 1_000:.1f}k"
    if n < 999_500:
        return f"{round(n / 1_000)}k"
    return f"{n / 1_000_000:.1f}M"


def clock(seconds: float) -> str:
    """A duration like a clock: 0:07, 23:10, 1:05:00."""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def money(usd: Decimal, gbp_per_usd: Decimal | None) -> str:
    """US dollars, shown in pounds if there's an exchange rate: £2.71, or
    <£0.01 for a fraction of a penny. Without one, in dollars: $3.59."""
    symbol, amount = ("$", usd) if gbp_per_usd is None else ("£", usd * gbp_per_usd)
    if 0 < amount < Decimal("0.01"):
        return f"<{symbol}0.01"
    return f"{symbol}{amount:.2f}"


# --- Git ----------------------------------------------------------------------------


def git_state(root: Path) -> GitState | None:
    """The branch `root` is on, and how many files have uncommitted changes.
    None if it isn't in a git repository, or git isn't installed.

    Two precautions, because this runs on its own, every few seconds, in
    whatever folder Wren was opened in:

      * GIT_OPTIONAL_LOCKS=0: `git status` normally refreshes the index as
        it goes, which takes a lock. A git command you run at the same
        moment would then fail with "index.lock exists". Editors set this
        for the same reason.
      * core.fsmonitor=false: a repository's own config can name a program
        for git to run during `status`. A folder you downloaded could use
        that to run anything; this switches it off, for this command only.
    """
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "status", "--porcelain=v2", "--branch"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError):  # no git, or it took too long
        return None
    if result.returncode != 0:  # not a repository
        return None
    return parse_git_status(result.stdout)


def parse_git_status(output: str) -> GitState:
    """Read `git status --porcelain=v2 --branch`. That format is made for
    programs: it's promised not to change between git versions, where the
    usual output is for people and may. Header lines start with "#"; every
    other line is one changed file."""
    branch, commit, uncommitted = "", "", 0
    for line in output.splitlines():
        if line.startswith("# branch.head "):
            branch = line.removeprefix("# branch.head ")
        elif line.startswith("# branch.oid "):
            commit = line.removeprefix("# branch.oid ")
        elif line and not line.startswith("#"):
            uncommitted += 1
    if branch == "(detached)":
        branch = f"detached at {commit[:7]}"
    return GitState(branch, uncommitted)
