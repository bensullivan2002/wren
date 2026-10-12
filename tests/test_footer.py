"""Tests for the footer's numbers (stats.py) and its rows (footer.py).

None of these run the app. RunStats is given events by hand, the way the
app hands them on from the agent, and a clock the test moves itself, so a
"two minute" session takes no time at all. The full-screen tests in
test_tui.py check that the app wires it all up.
"""

import importlib
import shutil
import subprocess
from decimal import Decimal

import pytest
from rich.text import Text

import wren.footer
from wren.agent import Agent, Compacted, ToolFinished, ToolStarted
from wren.config import DEFAULT_MODELS
from wren.footer import (
    AppState,
    GitState,
    Part,
    bar,
    clock,
    fit,
    footer_rows,
    git_state,
    money,
    parse_git_status,
    tokens,
)
from wren.messages import (
    Completed,
    Message,
    Prices,
    TextBlock,
    TextDelta,
    ToolUseBlock,
    Usage,
)
from wren.providers.fake import FakeProvider
from wren.stats import RunStats
from wren.tools import Permissions, ToolRegistry, default_tools
from wren.tools.base import ToolResult
from wren.tools.permissions import FileChange
from wren.tools.registry import DECLINED


class Clock:
    """A clock that only moves when the test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def completed(usage: Usage, model: str = "demo") -> Completed:
    return Completed(
        Message("assistant", [TextBlock("ok")]), "end_turn", usage, model=model
    )


def call(name: str = "read_file", **arguments) -> ToolUseBlock:
    return ToolUseBlock("t1", name, arguments or {"path": "a.txt"})


def styles(text: Text) -> set[str]:
    return {str(span.style) for span in text.spans}


# --- Laying out a row ---------------------------------------------------------------


def parts(*items: tuple[str, int]) -> list[Part]:
    return [Part(Text(text), keep) for text, keep in items]


def test_a_row_that_fits_shows_everything_with_its_separators():
    row = fit([parts(("a", 1), ("b", 1)), parts(("c", 1))], 80)
    assert row.plain == "a · b │ c"


def test_when_a_row_is_too_wide_the_least_important_parts_are_left_out():
    groups = [parts(("aaaa", 9)), parts(("bbbb", 1)), parts(("cccc", 8))]
    assert fit(groups, 18).plain == "aaaa │ bbbb │ cccc"  # exactly fits
    assert fit(groups, 17).plain == "aaaa │ cccc"  # no empty group left behind


def test_room_a_long_part_could_not_use_goes_to_a_shorter_one():
    groups = [parts(("aaaa", 9), ("a long part that will not fit", 8), ("bb", 1))]
    assert fit(groups, 9).plain == "aaaa · bb"


def test_among_equals_the_leftmost_part_is_kept():
    assert fit([parts(("aaaa", 5), ("bbbb", 5))], 6).plain == "aaaa"


def test_a_part_too_wide_on_its_own_is_cut_short():
    assert fit([parts(("abcdefghij", 9))], 5).plain == "abcd…"


def test_nothing_to_show_makes_an_empty_row():
    assert fit([], 40).plain == ""
    assert fit([[]], 40).plain == ""


# --- Formatting -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "n, shown",
    [
        (0, "0"),
        (980, "980"),
        (1_234, "1.2k"),
        (9_949, "9.9k"),
        (9_950, "10k"),  # not "10.0k"
        (52_000, "52k"),
        (999_499, "999k"),
        (999_500, "1.0M"),  # not "1000k"
        (1_100_000, "1.1M"),
    ],
)
def test_token_counts_are_short(n, shown):
    assert tokens(n) == shown


@pytest.mark.parametrize(
    "seconds, shown",
    [(0, "0:00"), (7, "0:07"), (59.9, "0:59"), (1390, "23:10"), (3900, "1:05:00")],
)
def test_durations_read_like_a_clock(seconds, shown):
    assert clock(seconds) == shown


def test_money_is_shown_in_pounds_when_there_is_a_rate():
    half = Decimal("0.5")
    assert money(Decimal(2), half) == "£1.00"
    assert money(Decimal(0), half) == "£0.00"
    assert money(Decimal("0.001"), half) == "<£0.01"  # spent, but less than a penny


def test_money_is_shown_in_dollars_until_there_is_a_rate():
    assert money(Decimal("3.5"), None) == "$3.50"
    assert money(Decimal("0.001"), None) == "<$0.01"


def test_the_spending_row_follows_the_rate():
    clock_, provider = Clock(), FakeProvider()
    stats = RunStats(clock_)
    stats.record(
        completed(Usage(1_000_000)), provider
    )  # $3 at the fake's made-up prices
    agent = Agent(provider, "sys", 100)
    assert "cost: $3.00" in rows(agent, stats)[2]
    assert "cost: £1.50" in rows(agent, stats, AppState(gbp_per_usd=Decimal("0.5")))[2]


def test_the_bar_fills_and_changes_colour():
    assert bar(0).plain == "▱" * 10
    assert bar(0.28).plain == "▰▰▰▱▱▱▱▱▱▱"
    assert bar(1.5).plain == "▰" * 10  # past the budget: full, not longer
    assert "green" in styles(bar(0.5))
    assert "yellow" in styles(bar(0.7))
    assert "red" in styles(bar(0.9))


def test_a_price_list_turns_tokens_into_dollars():
    prices = Prices(Decimal(3), Decimal(15), Decimal("0.3"), Decimal("3.75"))
    assert prices.cost(Usage(1_000_000, 1_000_000, 1_000_000, 1_000_000)) == Decimal(
        "22.05"
    )
    # Thinking tokens are part of the output tokens, so they add nothing of their own.
    assert prices.cost(Usage(output_tokens=1_000, thinking_tokens=800)) == Decimal(
        "0.015"
    )


def test_a_long_prompt_is_priced_higher_for_the_whole_call():
    long = Prices(Decimal(2), Decimal(10), Decimal("0.2"), Decimal("2.5"))
    prices = Prices(
        Decimal(1),
        Decimal(5),
        Decimal("0.1"),
        Decimal("1.25"),
        long_prompt_over=100_000,
        long_prompt=long,
    )
    # The prompt counts cache reads and writes too: 60k + 30k + 10k is 100k, not over.
    assert prices.cost(Usage(60_000, 1_000, 30_000, 10_000)) == Decimal("0.0805")
    # One more token and every token of the call, output too, is at the long price.
    assert prices.cost(Usage(60_001, 1_000, 30_000, 10_000)) == Decimal("0.161002")

    # Documented as dearer, but not by how much: unknown, never guessed.
    short_only = Prices(
        Decimal(1),
        Decimal(5),
        Decimal("0.1"),
        Decimal("1.25"),
        long_prompt_over=100_000,
    )
    assert short_only.cost(Usage(100_001)) is None


def test_a_call_too_long_to_price_is_counted_as_unpriced():
    stats, provider = RunStats(Clock()), FakeProvider()
    provider.prices = lambda: Prices(
        Decimal(1), Decimal(5), Decimal(0), Decimal(0), long_prompt_over=1_000
    )
    stats.record(completed(Usage(1_000)), provider)
    stats.record(completed(Usage(1_001)), provider)
    assert stats.cost == Decimal("0.001") and stats.unpriced_calls == 1


@pytest.mark.parametrize("name", ["anthropic", "openai"])
def test_each_provider_prices_its_default_model_from_a_stated_source(name):
    module = importlib.import_module(f"wren.providers.{name}")
    assert DEFAULT_MODELS[name] in module.DOCUMENTED_PRICES
    assert (
        module.PRICES_SOURCE.startswith("https://") and "2026" in module.PRICES_SOURCE
    )
    # Catches a value typed into the wrong column: on every model, on both
    # pricing pages, a cache read is the cheapest and output the dearest.
    for prices in module.DOCUMENTED_PRICES.values():
        for tier in filter(None, [prices, prices.long_prompt]):
            assert 0 < tier.cache_read < tier.input <= tier.cache_write < tier.output


def test_haiku_5_5_costs_more_once_its_prompt_is_over_100k():
    from wren.providers.anthropic import DOCUMENTED_PRICES

    haiku = DOCUMENTED_PRICES["claude-haiku-5-5"]
    assert haiku.cost(Usage(100_000)) == Decimal("0.01")  # $0.10 a million
    assert haiku.cost(Usage(100_001)) == Decimal(
        "0.0500005"
    )  # $0.50 a million, for all of it


# --- Git ----------------------------------------------------------------------------------


def test_git_status_gives_the_branch_and_the_files_not_yet_committed():
    output = (
        "# branch.oid 1a2b3c4d5e6f7a8b9c0d\n"
        "# branch.head main\n"
        "# branch.upstream origin/main\n"
        "# branch.ab +0 -0\n"
        "1 .M N... 100644 100644 100644 abc abc src/a.py\n"
        "? new.txt\n"
    )
    assert parse_git_status(output) == GitState("main", 2)


def test_a_detached_head_says_which_commit_it_is_on():
    output = "# branch.oid 1a2b3c4d5e6f7a8b9c0d\n# branch.head (detached)\n"
    assert parse_git_status(output) == GitState("detached at 1a2b3c4", 0)


@pytest.mark.skipif(shutil.which("git") is None, reason="git isn't installed")
def test_git_state_reads_a_real_repository(tmp_path, monkeypatch):
    # Stop git looking above tmp_path, in case the temporary folder is
    # itself inside a repository.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
    assert git_state(repo) == GitState("trunk", 0)  # no commits yet, nothing changed
    (repo / "new.txt").write_text("hi\n")
    assert git_state(repo) == GitState("trunk", 1)

    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    assert git_state(elsewhere) is None


# --- The numbers ------------------------------------------------------------------------


def test_a_turn_is_timed_and_its_steps_counted(tmp_path):
    clock_, provider = Clock(), FakeProvider()
    stats = RunStats(clock_)
    clock_.now += 5
    stats.start_turn()
    assert stats.turns == 1 and stats.in_turn and stats.step == 1

    clock_.now += 1.5
    stats.record(TextDelta("Hi"), provider)
    clock_.now += 1
    stats.record(TextDelta(" there"), provider)  # not the first word
    assert stats.first_word_seconds == 1.5
    stats.record(completed(Usage(1_000, 200)), provider)
    assert stats.step == 1

    write = call("write_file", path="a.txt", content="x")
    stats.record(ToolStarted(write), provider)
    assert stats.running_tool is write
    stats.record(
        ToolFinished(
            write, ToolResult("ok", change=FileChange(tmp_path / "a.txt", None, "x"))
        ),
        provider,
    )
    assert stats.running_tool is None and stats.step == 2  # waiting for the model again

    clock_.now += 2
    stats.record(completed(Usage(1_500, 100)), provider)
    clock_.now += 0.5
    stats.end_turn()

    assert not stats.in_turn and stats.turn_elapsed() is None
    assert stats.last_turn_seconds == 5.0
    assert stats.last_turn_tokens == 2_800
    assert stats.average_turn_tokens() == 2_800
    assert stats.elapsed() == 10.0  # from when Wren opened, not from the first turn
    assert stats.tool_calls == {"write_file": 1}
    assert stats.files_changed == {tmp_path / "a.txt"}


def test_the_average_is_over_every_finished_turn():
    stats, provider = RunStats(Clock()), FakeProvider()
    for spent in (1_000, 3_000):
        stats.start_turn()
        stats.record(completed(Usage(input_tokens=spent)), provider)
        stats.end_turn()
    assert stats.last_turn_tokens == 3_000 and stats.average_turn_tokens() == 2_000


def test_ending_a_turn_that_never_started_changes_nothing():
    # /compact ends the same way a turn does, but isn't one.
    stats = RunStats(Clock())
    stats.end_turn()
    assert stats.finished_turns == 0 and stats.last_turn_seconds is None


def test_a_no_from_you_is_not_a_tool_error():
    stats, provider = RunStats(Clock()), FakeProvider()
    write = call("write_file", path="a.txt", content="x")
    stats.record(
        ToolFinished(write, ToolResult(DECLINED, summary="declined", is_error=True)),
        provider,
    )
    assert stats.tool_errors == 0
    failed = ToolResult(
        "could not write",
        is_error=True,
        change=FileChange(write.input["path"], None, "x"),
    )
    stats.record(ToolFinished(write, failed), provider)
    assert stats.tool_errors == 1
    assert stats.files_changed == set()  # it failed, so nothing changed


def test_spending_is_priced_and_counted_per_minute():
    clock_, provider = (
        Clock(),
        FakeProvider(),
    )  # made-up prices: $3 in, $15 out, $0.30 cache read
    stats = RunStats(clock_)
    stats.record(completed(Usage(1_000_000, 100_000, 2_000_000)), provider)
    assert stats.cost == Decimal("5.1")
    assert stats.cache_hit() == pytest.approx(2 / 3)

    clock_.now += 59
    assert (
        stats.cost_per_minute() is None
    )  # too early: one reply would look like a fortune a minute
    clock_.now += 61
    assert stats.cost_per_minute() == Decimal("2.55")


def test_calls_with_unknown_prices_are_counted_but_not_priced():
    clock_, provider = Clock(), FakeProvider()
    stats = RunStats(clock_)
    provider.model = "demo-large"  # plays a model whose prices aren't known
    stats.record(completed(Usage(1_000, 100)), provider)
    clock_.now += 120
    assert stats.unpriced_calls == 1 and stats.cost == 0
    assert stats.cost_per_minute() is None  # "£0.00 a minute" would be untrue
    assert "cost: $? · burn: —" in spending_row(stats)
    agent = Agent(FakeProvider(), "sys", 100)
    assert (
        "cost: £? · burn: —"
        in rows(agent, stats, AppState(gbp_per_usd=Decimal("0.75")))[2]
    )

    provider.model = "demo"
    stats.record(completed(Usage(1_000_000)), provider)
    assert stats.cost == Decimal(3)
    assert "+?" in spending_row(stats)  # some of it is missing from the total


def test_compaction_counts_its_cost_even_when_it_fails():
    stats, provider = RunStats(Clock()), FakeProvider()
    stats.record(Compacted(100_000, 5_000, usage=Usage(100_000, 2_000)), provider)
    stats.record(
        Compacted(5_000, 5_000, error="the model refused", usage=Usage(5_000, 10)),
        provider,
    )
    assert stats.compactions == 1
    assert stats.usage == Usage(105_000, 2_010)

    provider.model = "demo-large"
    stats.record(Compacted(0, 0, error="nothing to compact"), provider)
    assert stats.unpriced_calls == 0  # no call was made, so there was nothing to price


def test_a_reply_from_another_model_is_noticed():
    stats, provider = RunStats(Clock()), FakeProvider()
    stats.record(completed(Usage(10), model="demo-2026-09-01"), provider)
    assert (
        stats.answered_by_another(provider) is None
    )  # a dated version of the same model
    stats.record(completed(Usage(10), model="something-else"), provider)
    assert stats.answered_by_another(provider) == "something-else"
    provider.model = "demo-fast"
    assert (
        stats.answered_by_another(provider) is None
    )  # that reply was to a different request


# --- The rows -------------------------------------------------------------------------------


def make_agent(tmp_path) -> Agent:
    tools = ToolRegistry(default_tools(), root=tmp_path, permissions=Permissions())
    return Agent(FakeProvider(), "sys", 100, tools, context_cap=150_000)


def rows(agent, stats, state=None, width=300) -> list[str]:
    """The footer as plain text. Wide by default, so nothing is left out:
    the project folder is a long temporary path."""
    return [row.plain for row in footer_rows(agent, stats, state or AppState(), width)]


def spending_row(stats) -> str:
    return rows(Agent(FakeProvider(), "sys", 100), stats)[2]


def test_the_idle_footer_shows_the_settings_and_the_keys(tmp_path):
    agent = make_agent(tmp_path)
    agent.context_tokens = 42_000
    state = AppState(
        git=GitState("[red]x[/]", 2), session_id="20261012-101500-ab12", resumed=True
    )
    shown = rows(agent, RunStats(Clock()), state)

    assert len(shown) == 6
    assert shown[0].startswith("model: demo · effort: default · provider: fake")
    assert (
        "git: [red]x[/] · uncommitted: 2" in shown[0]
    )  # a branch name is text, never markup
    assert "session id: 20261012-101500-ab12 (resumed)" in shown[0]
    assert "context: ▰▰▰▱▱▱▱▱▱▱ 28% · 42k/150k" in shown[1]
    assert "memory files: off" in shown[1]
    assert shown[5] == wren.footer.KEY_HINTS


def test_with_no_budget_the_context_row_says_why(tmp_path):
    agent = make_agent(tmp_path)
    agent.context_cap, agent.context_tokens = None, 42_000
    assert (
        "context: 42k (window not looked up yet)" in rows(agent, RunStats(Clock()))[1]
    )
    agent.look_up_context_window()
    assert (
        "context: ▰" in rows(agent, RunStats(Clock()))[1]
        and "/180k" in rows(agent, RunStats(Clock()))[1]
    )
    agent.provider.window = None
    agent.set_model("demo-fast")
    agent.look_up_context_window()
    assert (
        "context: 42k (window unknown: never compacted)"
        in rows(agent, RunStats(Clock()))[1]
    )
    agent.context_cap = 0
    assert "context: 42k (compaction off)" in rows(agent, RunStats(Clock()))[1]


def test_auto_mode_shows_in_the_settings_row_while_it_is_on(tmp_path):
    agent = make_agent(tmp_path)
    assert "auto: on" not in rows(agent, RunStats(Clock()))[0]
    agent.tools.permissions.auto = True
    assert rows(agent, RunStats(Clock()))[0].startswith(
        "model: demo · effort: default · provider: fake · auto: on"
    )


def test_a_narrow_terminal_keeps_every_row_on_one_line(tmp_path):
    agent = make_agent(tmp_path)
    for width in (100, 60, 30):
        shown = rows(agent, RunStats(Clock()), AppState(git=GitState("main", 0)), width)
        assert all(Text(row).cell_len <= width for row in shown)
        assert shown[0].startswith("model: demo")  # the first thing kept


def test_the_last_row_says_what_wren_is_doing(tmp_path):
    agent, stats, provider = make_agent(tmp_path), RunStats(Clock()), FakeProvider()
    busy = AppState(busy=True)
    assert (
        "working · esc to stop" in rows(agent, stats, busy)[5]
    )  # busy, but not a turn: /compact

    stats.start_turn()
    assert (
        "step 1 · waiting for the model · esc to stop" in rows(agent, stats, busy)[5]
    )  # no step limit by default
    stats.record(completed(Usage(10)), provider)
    stats.record(ToolStarted(call("read_file", path="a.txt")), provider)
    assert (
        "step 1 · running read_file(path='a.txt') · esc to stop"
        in rows(agent, stats, busy)[5]
    )
    agent.max_steps = 25  # with a limit, the footer shows how close it is
    assert (
        "step 1/25 · running read_file(path='a.txt') · esc to stop"
        in rows(agent, stats, busy)[5]
    )
    assert "stopping…" in rows(agent, stats, AppState(busy=True, stopping=True))[5]


def test_tools_are_listed_most_used_first(tmp_path):
    stats, provider = RunStats(Clock()), FakeProvider()
    for name in ("shell", "read_file", "read_file", "search", "read_file", "shell"):
        stats.record(ToolStarted(call(name)), provider)
    shown = rows(make_agent(tmp_path), stats)[4]
    assert shown.startswith(
        "tool calls: 6 · read_file 3 · shell 2 · search 1 │ files changed: 0"
    )


def test_a_reply_from_another_model_is_flagged_in_the_footer(tmp_path):
    agent, stats = make_agent(tmp_path), RunStats(Clock())
    stats.record(completed(Usage(10), model="something-else"), agent.provider)
    assert "⚠ last reply came from something-else" in rows(agent, stats)[0]
