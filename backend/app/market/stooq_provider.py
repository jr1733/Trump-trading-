"""Stooq CSV provider -- free, no API key, **daily bars only**.

Stooq serves a plain CSV at
``https://stooq.com/q/d/l/?s=aapl.us&i=d`` with columns
``Date,Open,High,Low,Close,Volume``. US symbols take a ``.us`` suffix; index
proxies differ, so the symbol map below handles the ones we quote as "market
context".

Because it has no intraday history, ``supports_intraday()`` is False and the
pipeline falls back to daily horizons. That is the documented Phase 1 behaviour,
not a bug.
"""

from __future__ import annotations

import csv
import datetime as dt
import io

import httpx

from ..config import settings
from .base import Bar, MarketDataError, MarketDataProvider
from .calendar import EASTERN, REGULAR_OPEN

# Stooq's symbol conventions for things that are not ordinary US equities.
_SYMBOL_OVERRIDES = {
    "VIX": "^vix",
    "TNX": "10yusy.b",  # US 10-year yield
    "SPX": "^spx",
    "DJI": "^dji",
    "NDX": "^ndx",
}


def _stooq_symbol(symbol: str) -> str:
    upper = symbol.upper()
    if upper in _SYMBOL_OVERRIDES:
        return _SYMBOL_OVERRIDES[upper]
    return f"{upper.lower()}.us"


class StooqMarketDataProvider(MarketDataProvider):
    name = "stooq"

    BASE_URL = "https://stooq.com/q/d/l/"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def supports_intraday(self) -> bool:
        return False

    def _get(self, params: dict[str, str]) -> str:
        headers = {"User-Agent": settings.http_user_agent}
        try:
            if self._client is not None:
                response = self._client.get(self.BASE_URL, params=params, headers=headers)
            else:
                with httpx.Client(timeout=settings.http_timeout_seconds) as client:
                    response = client.get(self.BASE_URL, params=params, headers=headers)
            response.raise_for_status()
            return response.text
        except httpx.HTTPError as exc:
            raise MarketDataError(f"stooq request failed: {exc}") from exc

    def fetch_daily(self, symbol: str, start: dt.date, end: dt.date) -> list[Bar]:
        body = self._get(
            {
                "s": _stooq_symbol(symbol),
                "i": "d",
                "d1": start.strftime("%Y%m%d"),
                "d2": end.strftime("%Y%m%d"),
            }
        )
        if not body.lstrip().lower().startswith("date"):
            # Stooq answers "No data" / an HTML error page with HTTP 200.
            raise MarketDataError(f"stooq returned no usable data for {symbol}: {body[:120]!r}")

        bars: list[Bar] = []
        for row in csv.DictReader(io.StringIO(body)):
            try:
                day = dt.date.fromisoformat(row["Date"])
                close = float(row["Close"])
                bars.append(
                    Bar(
                        symbol=symbol.upper(),
                        interval="1d",
                        ts=dt.datetime.combine(day, REGULAR_OPEN, tzinfo=EASTERN).astimezone(
                            dt.timezone.utc
                        ),
                        open=float(row["Open"]),
                        high=float(row["High"]),
                        low=float(row["Low"]),
                        close=close,
                        # Stooq's daily series is already split/dividend adjusted.
                        adjusted_close=close,
                        volume=float(row.get("Volume") or 0.0),
                    )
                )
            except (ValueError, KeyError, TypeError):
                # One malformed row must not discard the whole series.
                continue
        if not bars:
            raise MarketDataError(f"stooq returned no parseable rows for {symbol}")
        return bars

    def fetch_intraday(
        self, symbol: str, start: dt.datetime, end: dt.datetime, interval: str
    ) -> list[Bar]:
        raise MarketDataError("stooq does not provide intraday history")
