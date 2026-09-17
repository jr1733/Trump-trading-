"""Try one provider, fall back to another.

The point is availability, not redundancy for its own sake. Every source URL and
symbol mapping in this repository is an unverified guess, so a single provider
is a single point of failure for every number the app computes. With a fallback
configured, a Stooq outage or a moved endpoint degrades to slower, rate-limited
prices rather than to none.

Two things it deliberately does not do:

* **It does not merge.** Whichever provider answers first wins the whole
  request. Stitching a series together from two sources would mean bars from
  different vendors, differently adjusted, inside one return calculation --
  which shows up as a fake overnight gap exactly where the sources change over.
* **It does not retry the primary within a call.** If Stooq raised, the fallback
  answers. The next call tries Stooq again from scratch, so recovery is
  automatic without any half-open-circuit machinery to get wrong.

`supports_intraday()` is the AND of the chain, not the OR: a horizon must be
available whichever provider ends up answering, or the same event would get a
5-minute return one day and not the next.
"""

from __future__ import annotations

import datetime as dt
import logging

from .base import Bar, MarketDataError, MarketDataProvider

log = logging.getLogger(__name__)


class ChainedMarketDataProvider(MarketDataProvider):
    def __init__(self, primary: MarketDataProvider, fallback: MarketDataProvider) -> None:
        self.primary = primary
        self.fallback = fallback
        #: Which provider served the most recent successful call. `_store()`
        #: writes this onto every bar, so the data-quality page can show that
        #: the app has quietly been running on the fallback for a week.
        self.last_used = primary.name

    @property
    def name(self) -> str:
        # The name follows whichever provider actually answered, because it is
        # what gets written to `market_prices.provider`. A row labelled with the
        # chain rather than its real source would make the provenance useless.
        return self.last_used

    def supports_intraday(self) -> bool:
        return self.primary.supports_intraday() and self.fallback.supports_intraday()

    def intraday_intervals(self) -> list[str]:
        if not self.supports_intraday():
            return []
        shared = set(self.primary.intraday_intervals()) & set(self.fallback.intraday_intervals())
        return sorted(shared)

    def _attempt(self, call, *args, **kwargs):
        try:
            result = call(self.primary, *args, **kwargs)
            self.last_used = self.primary.name
            return result
        except MarketDataError as primary_error:
            log.warning(
                "market data: %s failed (%s); falling back to %s",
                self.primary.name,
                primary_error,
                self.fallback.name,
            )
            try:
                result = call(self.fallback, *args, **kwargs)
                self.last_used = self.fallback.name
                return result
            except MarketDataError as fallback_error:
                # Both names in the message: "no prices" with only the second
                # provider named sends you debugging the wrong one.
                raise MarketDataError(
                    f"both providers failed -- {self.primary.name}: {primary_error}; "
                    f"{self.fallback.name}: {fallback_error}"
                ) from fallback_error

    def fetch_daily(self, symbol: str, start: dt.date, end: dt.date) -> list[Bar]:
        return self._attempt(
            lambda provider: provider.fetch_daily(symbol, start, end)
        )

    def fetch_intraday(
        self, symbol: str, start: dt.datetime, end: dt.datetime, interval: str
    ) -> list[Bar]:
        return self._attempt(
            lambda provider: provider.fetch_intraday(symbol, start, end, interval)
        )
