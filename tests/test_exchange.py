"""Tests for the daily exchange rate (exchange.py).

None of these touch the network: each passes its own `fetch`, and its own
`today`, so a test decides what the "bank" says and what day it is.
"""

import json
from decimal import Decimal

from wren.exchange import CACHE_FILE, ExchangeRate, Rate


class Bank:
    """Plays Frankfurter: answers with `rate`, or raises `error`, and counts calls."""

    def __init__(self, rate="0.75555", date="2026-10-09", error=None):
        self.rate, self.date, self.error, self.calls = rate, date, error, 0

    def __call__(self):
        self.calls += 1
        if self.error:
            raise self.error
        return Decimal(self.rate), self.date


class Today:
    def __init__(self, day="2026-10-12"):
        self.day = day

    def __call__(self):
        return self.day


def test_with_no_saved_rate_there_is_none_until_a_lookup(tmp_path):
    exchange = ExchangeRate(tmp_path, Bank(), Today())
    assert exchange.gbp_per_usd is None  # so the footer shows dollars
    assert exchange.claim_lookup()
    assert exchange.look_up() == Rate(Decimal("0.75555"), "2026-10-09", "2026-10-12")
    assert exchange.gbp_per_usd == Decimal("0.75555")


def test_the_rate_is_saved_and_reused_all_day(tmp_path):
    bank, today = Bank(), Today()
    first = ExchangeRate(tmp_path, bank, today)
    assert first.claim_lookup()
    first.look_up()
    saved = json.loads((tmp_path / CACHE_FILE).read_text())
    assert saved["gbp_per_usd"] == "0.75555"  # a string, so no float rounding
    assert saved["looked_up_on"] == "2026-10-12" and saved["rate_date"] == "2026-10-09"

    later = ExchangeRate(tmp_path, bank, today)  # Wren opened again, same day
    assert later.gbp_per_usd == Decimal("0.75555")
    assert not later.claim_lookup()
    assert bank.calls == 1


def test_a_new_day_brings_a_new_lookup(tmp_path):
    bank, today = Bank(), Today()
    exchange = ExchangeRate(tmp_path, bank, today)
    assert exchange.claim_lookup()
    exchange.look_up()

    today.day = "2026-10-13"  # still open after midnight
    bank.rate, bank.date = "0.76", "2026-10-12"
    assert exchange.claim_lookup()
    exchange.look_up()
    assert exchange.rate == Rate(Decimal("0.76"), "2026-10-12", "2026-10-13")
    assert bank.calls == 2


def test_a_failed_lookup_keeps_the_last_rate_and_waits_until_tomorrow(tmp_path):
    bank, today = Bank(), Today("2026-10-11")
    ExchangeRate(tmp_path, bank, today).look_up()  # yesterday's, saved

    today.day = "2026-10-12"
    bank.error = OSError("no network")
    exchange = ExchangeRate(tmp_path, bank, today)
    assert exchange.claim_lookup()
    assert exchange.look_up().gbp_per_usd == Decimal(
        "0.75555"
    )  # yesterday's still shown
    assert not exchange.claim_lookup()  # one try a day, not one every ten seconds
    assert bank.calls == 2


def test_with_no_rate_ever_a_failed_lookup_leaves_dollars(tmp_path):
    for error in [OSError("timed out"), ValueError("not JSON"), KeyError("rates")]:
        exchange = ExchangeRate(tmp_path, Bank(error=error), Today())
        assert exchange.look_up() is None and exchange.gbp_per_usd is None
    assert not (tmp_path / CACHE_FILE).exists()


def test_an_implausible_rate_is_not_believed(tmp_path):
    for nonsense in ["0", "-1", "750"]:
        exchange = ExchangeRate(tmp_path, Bank(rate=nonsense), Today())
        assert exchange.look_up() is None
    assert not (tmp_path / CACHE_FILE).exists()


def test_a_damaged_cache_file_is_ignored(tmp_path):
    path = tmp_path / CACHE_FILE
    path.parent.mkdir(parents=True)
    for damaged in [
        "{not json",
        "[]",
        '{"gbp_per_usd": "abc", "rate_date": "x", "looked_up_on": "y"}',
        "{}",
    ]:
        path.write_text(damaged)
        assert ExchangeRate(tmp_path, Bank(), Today()).rate is None


def test_saving_leaves_no_temporary_files(tmp_path):
    exchange = ExchangeRate(tmp_path, Bank(), Today())
    exchange.look_up()
    assert [p.name for p in (tmp_path / ".wren").iterdir()] == ["exchange-rate.json"]
