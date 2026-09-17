"""Market data service: caching, return calculation and abnormal returns.

Everything the pipeline needs about prices goes through here so that:
  * bars are cached in ``market_prices`` and a provider outage degrades to
    "use what we already have" rather than failing the event;
  * a return always carries the *basis* it was computed on (intraday vs
    next-session-open), so two numbers labelled "1d" always mean the same thing.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..config import settings
from ..models import MarketPrice, Ticker
from . import calendar as mcal
from .base import Bar, MarketDataError, MarketDataProvider
from .mock_provider import MockMarketDataProvider
from .stooq_provider import StooqMarketDataProvider

log = logging.getLogger(__name__)

DAILY_HORIZONS = ["1d", "3d", "5d"]
INTRADAY_HORIZONS = ["1m", "5m", "15m", "30m", "60m"]
_HORIZON_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
_HORIZON_DAYS = {"1d": 1, "3d": 3, "5d": 5}


def build_provider(name: str | None = None) -> MarketDataProvider:
    key = (name or settings.market_data_provider).lower()
    if key == "stooq":
        return StooqMarketDataProvider()
    if key == "mock":
        return MockMarketDataProvider()
    log.warning("unknown market provider %r; falling back to mock", key)
    return MockMarketDataProvider()


@dataclass
class ReturnResult:
    """A single horizon's return for one ticker around one event."""

    horizon: str
    basis: str                      # intraday | next_open
    anchor_ts: dt.datetime
    anchor_price: float
    end_ts: dt.datetime
    end_price: float
    raw_return: float
    abnormal_return: float | None = None
    benchmark_return: float | None = None
    sector_return: float | None = None
    sector_abnormal_return: float | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "horizon": self.horizon,
            "basis": self.basis,
            "anchor_ts": self.anchor_ts.isoformat(),
            "anchor_price": self.anchor_price,
            "end_ts": self.end_ts.isoformat(),
            "end_price": self.end_price,
            "raw_return": self.raw_return,
            "abnormal_return": self.abnormal_return,
            "benchmark_return": self.benchmark_return,
            "sector_return": self.sector_return,
            "sector_abnormal_return": self.sector_abnormal_return,
            "notes": self.notes,
        }


class MarketDataService:
    def __init__(self, db: Session, provider: MarketDataProvider | None = None) -> None:
        self.db = db
        self.provider = provider or build_provider()
        # Per-instance close cache. A backtest or event study asks for the same
        # session's close thousands of times; without this, each one is a query.
        # Invalidated whenever bars are written, so it cannot go stale.
        self._close_cache: dict[tuple[str, dt.date], float | None] = {}
        # symbol -> the date span already known to be populated in this session.
        self._ensured: dict[str, tuple[dt.date, dt.date]] = {}
        # Memo for compute_returns. A backtest evaluates the same comparable
        # event against the same ticker once per observation; the answer cannot
        # change within one run.
        self._returns_cache: dict[tuple, dict[str, "ReturnResult"]] = {}

    def _note_covered(self, symbol: str, start: dt.date, end: dt.date) -> None:
        previous = self._ensured.get(symbol)
        if previous is None:
            self._ensured[symbol] = (start, end)
        else:
            self._ensured[symbol] = (min(previous[0], start), max(previous[1], end))

    # -- capability -------------------------------------------------------
    def available_horizons(self) -> list[str]:
        """Horizons we are willing to *claim* given the configured provider."""
        if self.provider.supports_intraday():
            return INTRADAY_HORIZONS + DAILY_HORIZONS
        return list(DAILY_HORIZONS)

    # -- cache ------------------------------------------------------------
    def _store(self, bars: list[Bar]) -> None:
        if not bars:
            return
        rows = [
            {
                "symbol": b.symbol,
                "interval": b.interval,
                "ts": b.ts,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "adjusted_close": b.adjusted_close,
                "volume": b.volume,
                "provider": self.provider.name,
            }
            for b in bars
        ]
        stmt = insert(MarketPrice).values(rows)
        # Idempotent: re-running a backfill refreshes rather than duplicates.
        stmt = stmt.on_conflict_do_update(
            constraint="uq_market_prices_sym_int_ts",
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "adjusted_close": stmt.excluded.adjusted_close,
                "volume": stmt.excluded.volume,
                "provider": stmt.excluded.provider,
            },
        )
        self.db.execute(stmt)
        self.db.flush()
        # New bars may change any close, and therefore any return, we have
        # already answered with.
        self._close_cache.clear()
        self._returns_cache.clear()

    def _cached_daily(self, symbol: str, start: dt.date, end: dt.date) -> list[MarketPrice]:
        stmt = (
            select(MarketPrice)
            .where(
                MarketPrice.symbol == symbol.upper(),
                MarketPrice.interval == "1d",
                MarketPrice.ts >= mcal.session_open_utc(start) - dt.timedelta(hours=12),
                MarketPrice.ts <= mcal.session_open_utc(end) + dt.timedelta(hours=12),
            )
            .order_by(MarketPrice.ts)
        )
        return list(self.db.execute(stmt).scalars())

    def ensure_daily(self, symbol: str, start: dt.date, end: dt.date) -> list[MarketPrice]:
        """Return cached daily bars, fetching from the provider when thin.

        A provider failure is logged and degraded to whatever is cached; callers
        decide what to do with too little data.
        """
        # A backtest calls this thousands of times with overlapping windows.
        # Remembering the span already satisfied for each symbol turns all but
        # the first of those into a no-op.
        key = symbol.upper()
        covered = self._ensured.get(key)
        if covered and covered[0] <= start and end <= covered[1]:
            return self._cached_daily(symbol, start, end)

        cached = self._cached_daily(symbol, start, end)
        expected = sum(
            1
            for i in range((end - start).days + 1)
            if mcal.is_trading_day(start + dt.timedelta(days=i))
        )
        if len(cached) >= max(expected - 1, 0) and cached:
            self._note_covered(key, start, end)
            return cached
        try:
            self._store(self.provider.fetch_daily(symbol, start, end))
        except MarketDataError as exc:
            log.warning("market data unavailable for %s: %s", symbol, exc)
            return cached
        except Exception as exc:  # defensive: a provider bug must not kill the run
            log.exception("unexpected market provider error for %s: %s", symbol, exc)
            return cached
        self._note_covered(key, start, end)
        return self._cached_daily(symbol, start, end)

    def ensure_intraday(
        self, symbol: str, start: dt.datetime, end: dt.datetime, interval: str
    ) -> list[MarketPrice]:
        if not self.provider.supports_intraday():
            return []
        try:
            self._store(self.provider.fetch_intraday(symbol, start, end, interval))
        except MarketDataError as exc:
            log.warning("intraday unavailable for %s: %s", symbol, exc)
            return []
        stmt = (
            select(MarketPrice)
            .where(
                MarketPrice.symbol == symbol.upper(),
                MarketPrice.interval == interval,
                MarketPrice.ts >= start,
                MarketPrice.ts <= end,
            )
            .order_by(MarketPrice.ts)
        )
        return list(self.db.execute(stmt).scalars())

    # -- price lookups ----------------------------------------------------
    def close_on_or_before(self, symbol: str, moment: dt.datetime) -> tuple[dt.datetime, float] | None:
        stmt = (
            select(MarketPrice)
            .where(
                MarketPrice.symbol == symbol.upper(),
                MarketPrice.interval == "1d",
                MarketPrice.ts <= moment,
            )
            .order_by(MarketPrice.ts.desc())
            .limit(1)
        )
        row = self.db.execute(stmt).scalars().first()
        return (row.ts, float(row.adjusted_close or row.close)) if row else None

    def close_on(self, symbol: str, day: dt.date) -> tuple[dt.datetime, float] | None:
        key = (symbol.upper(), day)
        if key in self._close_cache:
            price = self._close_cache[key]
            return (mcal.session_open_utc(day), price) if price is not None else None

        target = mcal.session_open_utc(day)
        stmt = select(MarketPrice).where(
            MarketPrice.symbol == symbol.upper(),
            MarketPrice.interval == "1d",
            MarketPrice.ts >= target - dt.timedelta(hours=12),
            MarketPrice.ts <= target + dt.timedelta(hours=12),
        )
        row = self.db.execute(stmt).scalars().first()
        if row is None:
            self._close_cache[key] = None
            return None
        price = float(row.adjusted_close or row.close)
        self._close_cache[key] = price
        return row.ts, price

    def daily_closes(self, symbol: str, start: dt.date, end: dt.date) -> dict[dt.date, float]:
        """Every cached daily close in a range, in one query.

        Callers that need a whole window (the event study fits a market model
        over ~180 sessions) must use this rather than looping `close_on`, which
        would issue one query per day per symbol.
        """
        rows = self.db.execute(
            select(MarketPrice.ts, MarketPrice.close, MarketPrice.adjusted_close).where(
                MarketPrice.symbol == symbol.upper(),
                MarketPrice.interval == "1d",
                MarketPrice.ts >= mcal.session_open_utc(start) - dt.timedelta(hours=12),
                MarketPrice.ts <= mcal.session_open_utc(end) + dt.timedelta(hours=12),
            )
        ).all()
        closes = {
            row.ts.astimezone(mcal.EASTERN).date(): float(row.adjusted_close or row.close)
            for row in rows
        }
        # Warm the per-day cache from the range query, so a later close_on()
        # for any of these days is free.
        for day, price in closes.items():
            self._close_cache[(symbol.upper(), day)] = price
        return closes

    def price_at(self, symbol: str, moment: dt.datetime, interval: str) -> tuple[dt.datetime, float] | None:
        stmt = (
            select(MarketPrice)
            .where(
                MarketPrice.symbol == symbol.upper(),
                MarketPrice.interval == interval,
                MarketPrice.ts <= moment,
            )
            .order_by(MarketPrice.ts.desc())
            .limit(1)
        )
        row = self.db.execute(stmt).scalars().first()
        return (row.ts, float(row.adjusted_close or row.close)) if row else None

    # -- returns ----------------------------------------------------------
    def sector_etf(self, symbol: str) -> str | None:
        row = self.db.get(Ticker, symbol.upper())
        return row.sector_etf if row else None

    def compute_returns(
        self,
        symbol: str,
        event_time: dt.datetime,
        horizons: list[str] | None = None,
        *,
        include_abnormal: bool = True,
    ) -> dict[str, ReturnResult]:
        """Returns for one ticker around one event time, keyed by horizon.

        Horizons the configured provider cannot support are silently omitted --
        the caller reports what came back, never a placeholder.
        """
        horizons = horizons or self.available_horizons()
        cache_key = (symbol.upper(), event_time, tuple(horizons), include_abnormal)
        memo = self._returns_cache.get(cache_key)
        if memo is not None:
            return memo

        anchor_ts, basis = mcal.anchor(event_time)
        results: dict[str, ReturnResult] = {}

        daily_wanted = [h for h in horizons if h in _HORIZON_DAYS]
        intraday_wanted = [
            h for h in horizons if h in _HORIZON_MINUTES and self.provider.supports_intraday()
        ]

        if daily_wanted:
            results.update(
                self._daily_returns(symbol, anchor_ts, basis, daily_wanted, include_abnormal)
            )
        if intraday_wanted:
            results.update(
                self._intraday_returns(symbol, anchor_ts, basis, intraday_wanted, include_abnormal)
            )
        self._returns_cache[cache_key] = results
        return results

    def _daily_returns(
        self,
        symbol: str,
        anchor_ts: dt.datetime,
        basis: str,
        horizons: list[str],
        include_abnormal: bool,
    ) -> dict[str, ReturnResult]:
        # `reaction_day` is the first session that can price the event in.
        # The daily baseline is the close *before* it, so the reaction day's own
        # move is inside the 1d return. See README "Return conventions".
        reaction_day = anchor_ts.astimezone(mcal.EASTERN).date()
        base_day = mcal.previous_trading_day(reaction_day)
        max_days = max(_HORIZON_DAYS[h] for h in horizons)
        window_start = base_day - dt.timedelta(days=10)
        window_end = reaction_day + dt.timedelta(days=max_days * 2 + 10)

        symbols = [symbol.upper()]
        sector = self.sector_etf(symbol) if include_abnormal else None
        if include_abnormal:
            symbols.append(settings.benchmark_symbol.upper())
        if sector:
            symbols.append(sector.upper())
        for sym in dict.fromkeys(symbols):
            self.ensure_daily(sym, window_start, window_end)

        base = self.close_on(symbol, base_day)
        if base is None:
            return {}
        base_ts, base_price = base

        out: dict[str, ReturnResult] = {}
        for horizon in horizons:
            # "1d" = the reaction session itself; "3d" = three sessions including it.
            sessions = _HORIZON_DAYS[horizon]
            end_day = (
                reaction_day
                if sessions == 1
                else mcal.trading_days_after(reaction_day, sessions - 1)
            )
            end = self.close_on(symbol, end_day)
            if end is None or base_price <= 0:
                continue
            end_ts, end_price = end
            raw = (end_price / base_price) - 1.0

            result = ReturnResult(
                horizon=horizon,
                basis=basis,
                anchor_ts=base_ts,
                anchor_price=base_price,
                end_ts=end_ts,
                end_price=end_price,
                raw_return=raw,
            )
            if basis == "next_open":
                result.notes.append(
                    f"Event fell outside regular trading hours; first session that "
                    f"could react is {reaction_day.isoformat()}."
                )
            if include_abnormal:
                bench = self._simple_daily_return(settings.benchmark_symbol, base_day, end_day)
                if bench is not None:
                    result.benchmark_return = bench
                    result.abnormal_return = raw - bench
                if sector:
                    sec = self._simple_daily_return(sector, base_day, end_day)
                    if sec is not None:
                        result.sector_return = sec
                        result.sector_abnormal_return = raw - sec
            out[horizon] = result
        return out

    def _simple_daily_return(self, symbol: str, start_day: dt.date, end_day: dt.date) -> float | None:
        start = self.close_on(symbol, start_day)
        end = self.close_on(symbol, end_day)
        if not start or not end or start[1] <= 0:
            return None
        return (end[1] / start[1]) - 1.0

    def _intraday_returns(
        self,
        symbol: str,
        anchor_ts: dt.datetime,
        basis: str,
        horizons: list[str],
        include_abnormal: bool,
    ) -> dict[str, ReturnResult]:
        max_minutes = max(_HORIZON_MINUTES[h] for h in horizons)
        end_window = anchor_ts + dt.timedelta(minutes=max_minutes + 5)
        interval = "1m"
        symbols = [symbol.upper()]
        if include_abnormal:
            symbols.append(settings.benchmark_symbol.upper())
        for sym in dict.fromkeys(symbols):
            self.ensure_intraday(sym, anchor_ts - dt.timedelta(minutes=5), end_window, interval)

        base = self.price_at(symbol, anchor_ts, interval)
        if base is None:
            return {}
        base_ts, base_price = base

        out: dict[str, ReturnResult] = {}
        for horizon in horizons:
            target = anchor_ts + dt.timedelta(minutes=_HORIZON_MINUTES[horizon])
            # Crossing the closing bell means the horizon does not exist for this
            # event; we omit it rather than stretch it to the next session.
            if mcal.classify(target) != "regular":
                continue
            end = self.price_at(symbol, target, interval)
            if end is None or base_price <= 0:
                continue
            end_ts, end_price = end
            raw = (end_price / base_price) - 1.0
            result = ReturnResult(
                horizon=horizon,
                basis=basis,
                anchor_ts=base_ts,
                anchor_price=base_price,
                end_ts=end_ts,
                end_price=end_price,
                raw_return=raw,
            )
            if include_abnormal:
                bench_base = self.price_at(settings.benchmark_symbol, anchor_ts, interval)
                bench_end = self.price_at(settings.benchmark_symbol, target, interval)
                if bench_base and bench_end and bench_base[1] > 0:
                    bench = (bench_end[1] / bench_base[1]) - 1.0
                    result.benchmark_return = bench
                    result.abnormal_return = raw - bench
            out[horizon] = result
        return out
