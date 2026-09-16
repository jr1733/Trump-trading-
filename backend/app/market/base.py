"""Market data provider interface.

`supports_intraday()` is the capability flag that decides whether the intraday
horizons (1/5/15/30/60 min) are computed at all. A provider that only returns
daily bars must return False, and the pipeline then reports daily horizons and
labels them -- rather than fabricating a 5-minute return from a daily close.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class Bar:
    symbol: str
    interval: str
    ts: dt.datetime  # UTC, bar start
    open: float
    high: float
    low: float
    close: float
    adjusted_close: float | None
    volume: float


class MarketDataError(RuntimeError):
    """Raised for provider failures. Callers degrade; they never crash."""


class MarketDataProvider:
    name = "base"

    def supports_intraday(self) -> bool:
        return False

    def intraday_intervals(self) -> list[str]:
        return []

    def fetch_daily(self, symbol: str, start: dt.date, end: dt.date) -> list[Bar]:
        raise NotImplementedError

    def fetch_intraday(
        self, symbol: str, start: dt.datetime, end: dt.datetime, interval: str
    ) -> list[Bar]:
        raise NotImplementedError
