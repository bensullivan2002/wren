"""The day's exchange rate from US dollars to pounds, for the footer's costs.

Providers price in dollars. The first time the app opens each day, Wren asks
Frankfurter, a free service that republishes the European Central Bank's
reference rates, for the day's rate, and saves it in:

    ~/.wren/exchange-rate.json

Every later run that day uses the saved rate without asking. If the lookup
fails, the last saved rate is used, however old. If there has never been
one, costs are shown in dollars, as the providers price them.

The bank publishes on working days only, so on a Saturday the newest rate is
Friday's: `rate_date` says which day the rate is for, and `looked_up_on`
says when Wren asked. The cache is good until `looked_up_on` isn't today.

The request carries nothing about you, your code or your conversation: it's
a fixed URL. It's the one network call Wren makes that isn't to the model.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Callable

RATE_URL = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=GBP"
SOURCE = "European Central Bank reference rate, via frankfurter.dev"
TIMEOUT = 5  # seconds: it runs on a worker, but there's no point waiting long
CACHE_FILE = Path(".wren") / "exchange-rate.json"  # under the home directory

# Whatever goes wrong with a lookup (no network, a timeout, an error page,
# JSON that isn't the shape expected, a number that isn't one) means the
# same thing: no new rate today. Each is one of these.
LOOKUP_FAILED = (OSError, ValueError, KeyError, TypeError, ArithmeticError)


@dataclass(frozen=True)
class Rate:
    gbp_per_usd: Decimal
    rate_date: str  # the day the bank published it, "2026-10-09"
    looked_up_on: str  # the day Wren asked for it


def fetch_rate() -> tuple[Decimal, str]:
    """Ask Frankfurter for the rate: pounds per dollar, and the date it's for.
    Blocks for up to TIMEOUT seconds, and raises one of LOOKUP_FAILED."""
    request = urllib.request.Request(RATE_URL, headers={"User-Agent": "wren"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        data = json.load(response)
    # str() first: Decimal(0.75555), from a float, would carry the float's
    # binary error along (0.755549999...).
    return Decimal(str(data["rates"]["GBP"])), str(data["date"])


class ExchangeRate:
    """Holds the rate, and knows when to look up a new one.

    `fetch` and `today` are arguments so that tests pass their own: a test
    never touches the network, and decides what day it is.
    """

    def __init__(
        self,
        home: Path | None = None,
        fetch: Callable[[], tuple[Decimal, str]] = fetch_rate,
        today: Callable[[], str] = lambda: date.today().isoformat(),
    ) -> None:
        self.path = (home if home is not None else Path.home()) / CACHE_FILE
        self.fetch = fetch
        self.today = today
        self.rate: Rate | None = self._load()
        self._tried_on: str | None = None  # at most one attempt a day, even if it fails

    @property
    def gbp_per_usd(self) -> Decimal | None:
        return self.rate.gbp_per_usd if self.rate else None

    def claim_lookup(self) -> bool:
        """Whether a lookup is due: no rate yet today, and none tried today.
        Call on the app thread; True also marks today as tried, so a second
        call can't start a second lookup while the first is under way."""
        today = self.today()
        if (self.rate and self.rate.looked_up_on == today) or self._tried_on == today:
            return False
        self._tried_on = today
        return True

    def look_up(self) -> Rate | None:
        """Fetch today's rate and save it. BLOCKS: run it on a worker thread.
        On failure, keeps the rate it had. Returns the rate either way."""
        try:
            gbp_per_usd, rate_date = self.fetch()
            if not 0 < gbp_per_usd < 10:
                raise ValueError(f"not a plausible rate: {gbp_per_usd}")
        except LOOKUP_FAILED:
            return self.rate
        self.rate = Rate(gbp_per_usd, rate_date, self.today())
        try:
            self._save()
        except OSError:
            pass  # still good for this run; the next run asks again
        return self.rate

    def _load(self) -> Rate | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            rate = Rate(Decimal(data["gbp_per_usd"]), str(data["rate_date"]), str(data["looked_up_on"]))
        except (*LOOKUP_FAILED, AttributeError):  # missing, unreadable, or not what we wrote
            return None
        return rate if 0 < rate.gbp_per_usd < 10 else None

    def _save(self) -> None:
        # Write to a temporary file, then rename it over the old one. A
        # rename is all-or-nothing, so two Wrens starting at once, or one
        # killed half-way, can't leave a half-written file.
        assert self.rate is not None
        data = {
            "gbp_per_usd": str(self.rate.gbp_per_usd),  # a string: JSON numbers are floats
            "rate_date": self.rate.rate_date,
            "looked_up_on": self.rate.looked_up_on,
            "source": SOURCE,
            "url": RATE_URL,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".exchange-rate-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
