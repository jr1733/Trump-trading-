"""Event studies: market-model abnormal returns and CARs.

This is the statistically proper version of the "historical response" numbers.
Phase 1 subtracted the benchmark's raw return, which implicitly assumes beta is
exactly 1. Here we estimate the market model per ticker,

    R_i,t = alpha + beta * R_m,t + e_i,t

over an estimation window that ends `gap` trading days *before* the event, so
the pre-event run-up cannot contaminate alpha and beta. Then

    AR_t  = R_i,t - (alpha + beta * R_m,t)
    CAR   = sum of AR over the event window

Significance uses the estimation-window residual standard deviation, which is
the standard approach (Brown & Warner). Across several events we report the
cumulative average abnormal return (CAAR) and a cross-sectional t-statistic.

Three honesty rules are enforced in code, not left to the reader:

* every window is bounded by the event timestamp (no look-ahead);
* a model estimated on fewer than `EVENT_STUDY_MIN_OBSERVATIONS` days is
  refused rather than reported with a wide error bar;
* a t-statistic is only reported when N is large enough for it to mean
  anything, and the sample-size flag travels with every number.

No numpy: a two-variable OLS is four sums, and the dependency would cost more
than it saves on a small VPS.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import statistics
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..market import calendar as mcal
from ..market.service import MarketDataService
from ..models import Event, EventTicker
from .historical import sample_flag

log = logging.getLogger(__name__)

# Event windows, as (start, end) offsets in trading days from the reaction day.
DEFAULT_WINDOWS = [(0, 0), (0, 1), (0, 4), (-1, 1), (0, 19)]


def window_label(window: tuple[int, int]) -> str:
    start, end = window
    return f"[{start:+d},{end:+d}]"


@dataclass
class MarketModel:
    alpha: float
    beta: float
    residual_sd: float
    observations: int
    r_squared: float

    def expected(self, market_return: float) -> float:
        return self.alpha + self.beta * market_return

    def as_dict(self) -> dict:
        return {
            "alpha": round(self.alpha, 6),
            "beta": round(self.beta, 4),
            "residual_sd": round(self.residual_sd, 6),
            "observations": self.observations,
            "r_squared": round(self.r_squared, 4),
        }


@dataclass
class EventStudyResult:
    ticker: str
    event_type: str | None
    n_events: int
    sample_flag: str
    windows: dict[str, dict] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    benchmark: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "event_type": self.event_type,
            "n_events": self.n_events,
            "sample_flag": self.sample_flag,
            "benchmark": self.benchmark,
            "windows": self.windows,
            "events": self.events,
            "skipped": self.skipped,
            "notes": self.notes,
        }


def ordinary_least_squares(xs: list[float], ys: list[float]) -> MarketModel | None:
    """Simple OLS of y on x. Returns None when the fit is not identified."""
    n = len(xs)
    if n != len(ys) or n < 3:
        return None
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        # The benchmark did not move at all over the window: beta is not
        # identified, and pretending otherwise would be fabrication.
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    beta = sxy / sxx
    alpha = mean_y - beta * mean_x

    residuals = [y - (alpha + beta * x) for x, y in zip(xs, ys)]
    # n-2 degrees of freedom: two parameters estimated.
    residual_sd = math.sqrt(sum(r * r for r in residuals) / (n - 2)) if n > 2 else 0.0

    syy = sum((y - mean_y) ** 2 for y in ys)
    r_squared = 1.0 - (sum(r * r for r in residuals) / syy) if syy > 0 else 0.0
    return MarketModel(alpha, beta, residual_sd, n, r_squared)


def _daily_return_series(
    market: MarketDataService, symbol: str, days: list[dt.date]
) -> dict[dt.date, float]:
    """Close-to-close returns keyed by the later of the two sessions.

    One range query, not one per day: a market model over 180 sessions across
    27 events would otherwise issue roughly ten thousand single-row lookups.
    """
    if not days:
        return {}
    closes = market.daily_closes(symbol, days[0], days[-1])

    returns: dict[dt.date, float] = {}
    previous: tuple[dt.date, float] | None = None
    for day in days:
        price = closes.get(day)
        if price is None:
            continue
        if previous is not None and previous[1] > 0:
            returns[day] = (price / previous[1]) - 1.0
        previous = (day, price)
    return returns


def trading_days_between(start: dt.date, end: dt.date) -> list[dt.date]:
    days: list[dt.date] = []
    cursor = start
    while cursor <= end:
        if mcal.is_trading_day(cursor):
            days.append(cursor)
        cursor += dt.timedelta(days=1)
    return days


def estimate_market_model(
    market: MarketDataService,
    ticker: str,
    reaction_day: dt.date,
    *,
    benchmark: str | None = None,
    estimation_days: int | None = None,
    gap_days: int | None = None,
) -> tuple[MarketModel | None, str | None]:
    """Fit the market model on the window strictly before the event.

    Returns `(model, reason_if_none)` so the caller can report *why* an event
    was skipped instead of silently dropping it.
    """
    benchmark = (benchmark or settings.benchmark_symbol).upper()
    estimation_days = estimation_days or settings.event_study_estimation_days
    gap_days = gap_days or settings.event_study_gap_days

    window_end = reaction_day
    for _ in range(gap_days):
        window_end = mcal.previous_trading_day(window_end)
    # Calendar span generously wider than the trading-day count we need.
    window_start = window_end - dt.timedelta(days=int(estimation_days * 1.6) + 10)

    market.ensure_daily(ticker, window_start, window_end)
    market.ensure_daily(benchmark, window_start, window_end)

    days = trading_days_between(window_start, window_end)[-(estimation_days + 1) :]
    stock = _daily_return_series(market, ticker, days)
    index = _daily_return_series(market, benchmark, days)

    shared = sorted(set(stock) & set(index))
    if len(shared) < settings.event_study_min_observations:
        return None, (
            f"only {len(shared)} overlapping return observations in the estimation "
            f"window (need {settings.event_study_min_observations})"
        )

    model = ordinary_least_squares([index[d] for d in shared], [stock[d] for d in shared])
    if model is None:
        return None, "market model could not be identified on this window"
    return model, None


def abnormal_returns(
    market: MarketDataService,
    ticker: str,
    reaction_day: dt.date,
    model: MarketModel,
    *,
    benchmark: str | None = None,
    max_offset: int = 20,
    min_offset: int = -5,
) -> dict[int, float]:
    """Abnormal return per trading-day offset from the reaction day."""
    benchmark = (benchmark or settings.benchmark_symbol).upper()

    start = reaction_day - dt.timedelta(days=abs(min_offset) * 2 + 10)
    end = reaction_day + dt.timedelta(days=max_offset * 2 + 10)
    market.ensure_daily(ticker, start, end)
    market.ensure_daily(benchmark, start, end)

    days = trading_days_between(start, end)
    if reaction_day not in days:
        return {}
    pivot = days.index(reaction_day)

    stock = _daily_return_series(market, ticker, days)
    index = _daily_return_series(market, benchmark, days)

    out: dict[int, float] = {}
    for offset in range(min_offset, max_offset + 1):
        position = pivot + offset
        if position < 0 or position >= len(days):
            continue
        day = days[position]
        if day not in stock or day not in index:
            continue
        out[offset] = stock[day] - model.expected(index[day])
    return out


def cumulative(ars: dict[int, float], window: tuple[int, int]) -> float | None:
    start, end = window
    values = [ars[offset] for offset in range(start, end + 1) if offset in ars]
    if len(values) != (end - start + 1):
        return None  # incomplete window: reporting a partial CAR would mislead
    return sum(values)


def car_t_statistic(car: float, model: MarketModel, days: int) -> float | None:
    """t = CAR / (residual sd * sqrt(days)), the standard Brown-Warner form."""
    if model.residual_sd <= 0 or days <= 0:
        return None
    return car / (model.residual_sd * math.sqrt(days))


def run_event_study(
    db: Session,
    market: MarketDataService,
    *,
    ticker: str,
    event_type: str | None = None,
    before: dt.datetime | None = None,
    windows: list[tuple[int, int]] | None = None,
    limit: int = 200,
) -> EventStudyResult:
    """Event study over past events touching `ticker`.

    `before` bounds the sample: nothing at or after it enters the study.
    """
    ticker = ticker.upper()
    windows = windows or DEFAULT_WINDOWS
    benchmark = settings.benchmark_symbol.upper()

    stmt = (
        select(Event)
        .join(EventTicker, EventTicker.event_id == Event.id)
        .where(
            EventTicker.ticker == ticker,
            EventTicker.confidence.in_(["HIGH", "MEDIUM"]),
        )
        .order_by(Event.source_timestamp.desc())
        .limit(limit)
    )
    if event_type:
        stmt = stmt.where(Event.event_type == event_type)
    if before is not None:
        stmt = stmt.where(Event.source_timestamp < before)

    events = list(db.execute(stmt).scalars().unique())

    result = EventStudyResult(
        ticker=ticker,
        event_type=event_type,
        n_events=0,
        sample_flag="unreliable",
        benchmark=benchmark,
    )
    if not market.provider.supports_intraday():
        result.notes.append(
            "Daily bars only: abnormal returns are measured session by session, "
            "so the event-day figure includes the whole session's move."
        )

    per_window: dict[str, list[float]] = {window_label(w): [] for w in windows}
    per_window_t: dict[str, list[float]] = {window_label(w): [] for w in windows}

    for event in events:
        anchor_ts, basis = mcal.anchor(event.source_timestamp)
        reaction_day = anchor_ts.astimezone(mcal.EASTERN).date()

        model, reason = estimate_market_model(market, ticker, reaction_day, benchmark=benchmark)
        if model is None:
            result.skipped.append(
                {
                    "event_id": event.id,
                    "source_timestamp": event.source_timestamp.isoformat(),
                    "reason": reason,
                }
            )
            continue

        ars = abnormal_returns(market, ticker, reaction_day, model, benchmark=benchmark)
        if not ars:
            result.skipped.append(
                {
                    "event_id": event.id,
                    "source_timestamp": event.source_timestamp.isoformat(),
                    "reason": "no price data around the event",
                }
            )
            continue

        entry = {
            "event_id": event.id,
            "title": event.title or (event.text[:100] if event.text else ""),
            "source_timestamp": event.source_timestamp.isoformat(),
            "reaction_day": reaction_day.isoformat(),
            "basis": basis,
            "event_type": event.event_type,
            "model": model.as_dict(),
            "car": {},
        }
        for window in windows:
            label = window_label(window)
            car = cumulative(ars, window)
            if car is None:
                continue
            days = window[1] - window[0] + 1
            t_stat = car_t_statistic(car, model, days)
            entry["car"][label] = {
                "value": round(car, 6),
                "t_stat": round(t_stat, 3) if t_stat is not None else None,
            }
            per_window[label].append(car)
            if t_stat is not None:
                per_window_t[label].append(t_stat)

        result.events.append(entry)

    result.n_events = len(result.events)
    result.sample_flag = sample_flag(result.n_events)

    for label, values in per_window.items():
        if not values:
            continue
        n = len(values)
        mean = statistics.fmean(values)
        sd = statistics.stdev(values) if n > 1 else 0.0
        # Cross-sectional t: does the average abnormal return differ from zero
        # across events? Only meaningful with a handful of events at minimum.
        t_stat = (mean / (sd / math.sqrt(n))) if (n >= 5 and sd > 0) else None
        result.windows[label] = {
            "window": label,
            "n": n,
            "caar": round(mean, 6),
            "median_car": round(statistics.median(values), 6),
            "sd": round(sd, 6),
            "positive_pct": round(100.0 * sum(1 for v in values if v > 0) / n, 2),
            "t_stat": round(t_stat, 3) if t_stat is not None else None,
            "significant_5pct": bool(t_stat is not None and abs(t_stat) > 1.96),
            "flag": sample_flag(n),
        }

    if result.n_events < settings.min_usable_sample:
        result.notes.append(
            f"Only {result.n_events} events cleared the market-model requirements; "
            "treat every figure here as illustrative rather than evidential."
        )
    if result.skipped:
        result.notes.append(
            f"{len(result.skipped)} event(s) were skipped for want of a usable "
            "estimation window or price data. They are listed so the sample is auditable."
        )
    result.notes.append(
        "Abnormal returns are relative to a market model estimated on the "
        f"{settings.event_study_estimation_days} trading days ending "
        f"{settings.event_study_gap_days} sessions before each event."
    )
    return result
