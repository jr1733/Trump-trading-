"""Deterministic synthetic market data.

Seeded per (symbol, date) so the same bar comes back on every run and every
machine -- tests that assert on returns need this to be reproducible.

The generator is a plain random walk with a per-symbol drift and volatility.
It is *not* meant to look like a real price series; it exists so the pipeline,
the statistics and the UI can be exercised end to end with no API key.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math

from .base import Bar, MarketDataProvider
from .calendar import EASTERN, REGULAR_OPEN, is_trading_day, session_close

_BASE_PRICES = {
    "SPY": 540.0, "QQQ": 470.0, "AAPL": 225.0, "MSFT": 420.0, "NVDA": 120.0,
    "TSLA": 250.0, "AMZN": 185.0, "META": 500.0, "GOOGL": 170.0, "JPM": 210.0,
    "BA": 180.0, "CAT": 340.0, "DE": 400.0, "F": 11.0, "GM": 45.0,
    "XLE": 92.0, "XLF": 43.0, "XLK": 230.0, "XLI": 130.0, "XLV": 145.0,
    "VIX": 15.0, "TNX": 4.2, "DJT": 30.0, "LMT": 460.0, "RTX": 115.0,
    "STLA": 18.0, "TM": 190.0, "TSM": 175.0, "INTC": 30.0, "MU": 105.0,
}
_DEFAULT_BASE = 100.0


def _unit(symbol: str, key: str) -> float:
    """Deterministic pseudo-uniform in [0, 1) from a symbol + key."""
    digest = hashlib.sha256(f"{symbol}|{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _gauss(symbol: str, key: str) -> float:
    """Box-Muller on two deterministic uniforms -> pseudo-normal."""
    u1 = max(_unit(symbol, key + "|a"), 1e-9)
    u2 = _unit(symbol, key + "|b")
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


class MockMarketDataProvider(MarketDataProvider):
    name = "mock"

    EPOCH = dt.date(2023, 1, 3)

    def __init__(self, daily_vol: float = 0.012) -> None:
        self.daily_vol = daily_vol
        # symbol -> {date: close}, grown lazily. The walk is cumulative, so we
        # memoise rather than re-summing from the epoch on every lookup.
        self._series: dict[str, dict[dt.date, float]] = {}

    def supports_intraday(self) -> bool:
        return True

    def intraday_intervals(self) -> list[str]:
        return ["1m", "5m", "15m", "30m", "60m"]

    # -- daily ------------------------------------------------------------
    def _close_for(self, symbol: str, day: dt.date) -> float:
        """Cumulative walk from a fixed epoch so adjacent closes are related."""
        key = symbol.upper()
        series = self._series.setdefault(key, {})
        if day in series:
            return series[day]

        if series:
            cursor = max(series)
            log_price = math.log(series[cursor])
        else:
            cursor = self.EPOCH - dt.timedelta(days=1)
            log_price = math.log(_BASE_PRICES.get(key, _DEFAULT_BASE))

        if day <= cursor:
            # Requested a day before anything cached: rebuild from the epoch.
            cursor = self.EPOCH - dt.timedelta(days=1)
            log_price = math.log(_BASE_PRICES.get(key, _DEFAULT_BASE))

        while cursor < day:
            cursor += dt.timedelta(days=1)
            if is_trading_day(cursor):
                log_price += (
                    self.daily_vol * _gauss(key, cursor.isoformat()) - 0.5 * self.daily_vol**2
                )
            series[cursor] = round(math.exp(log_price), 4)
        return series[day]

    def fetch_daily(self, symbol: str, start: dt.date, end: dt.date) -> list[Bar]:
        bars: list[Bar] = []
        day = start
        while day <= end:
            if is_trading_day(day):
                close = self._close_for(symbol, day)
                spread = close * 0.004 * (0.5 + _unit(symbol, f"{day}|spread"))
                open_ = round(close - spread * _gauss(symbol, f"{day}|open") * 0.3, 4)
                bars.append(
                    Bar(
                        symbol=symbol.upper(),
                        interval="1d",
                        ts=dt.datetime.combine(day, REGULAR_OPEN, tzinfo=EASTERN).astimezone(
                            dt.timezone.utc
                        ),
                        open=open_,
                        high=round(max(open_, close) + spread, 4),
                        low=round(min(open_, close) - spread, 4),
                        close=close,
                        adjusted_close=close,
                        volume=round(1_000_000 * (0.5 + _unit(symbol, f"{day}|vol")), 2),
                    )
                )
            day += dt.timedelta(days=1)
        return bars

    # -- intraday ---------------------------------------------------------
    def fetch_intraday(
        self, symbol: str, start: dt.datetime, end: dt.datetime, interval: str
    ) -> list[Bar]:
        minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}[interval]
        step = dt.timedelta(minutes=minutes)
        intraday_vol = self.daily_vol / math.sqrt(390 / minutes)

        bars: list[Bar] = []
        cursor = start.astimezone(dt.timezone.utc).replace(second=0, microsecond=0)
        while cursor <= end:
            local = cursor.astimezone(EASTERN)
            if is_trading_day(local.date()) and REGULAR_OPEN <= local.time() < session_close(
                local.date()
            ):
                day_close = self._close_for(symbol, local.date())
                elapsed = (
                    local.hour * 60 + local.minute - (REGULAR_OPEN.hour * 60 + REGULAR_OPEN.minute)
                )
                drift = intraday_vol * _gauss(symbol, f"{cursor.isoformat()}|i") * math.sqrt(
                    max(elapsed, 1) / 390
                )
                close = round(day_close * math.exp(drift), 4)
                bars.append(
                    Bar(
                        symbol=symbol.upper(),
                        interval=interval,
                        ts=cursor,
                        open=close,
                        high=round(close * 1.0008, 4),
                        low=round(close * 0.9992, 4),
                        close=close,
                        adjusted_close=close,
                        volume=round(5_000 * (0.5 + _unit(symbol, f"{cursor}|v")), 2),
                    )
                )
            cursor += step
        return bars
