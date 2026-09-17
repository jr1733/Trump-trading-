"""Backtesting -- deliberately separate from live signals.

What this measures: for past events whose signal cleared a threshold, what did
the ticker do over the following N sessions, in the direction the signal
pointed. It is a **research statistic**, not a simulated trading account: there
is no capital, no position sizing, no costs, no slippage and no execution model,
and the code does not pretend otherwise.

Three things this module takes seriously, because a backtest that gets them
wrong is worse than no backtest:

**1. Look-ahead prevention.** Every signal is recomputed point-in-time. The
comparable-event sample, the similarity search and the novelty window are all
bounded by the subject event's own timestamp, so a 2024 signal is built only
from what existed in 2024. Signal weights are static configuration, never fitted
here -- if you ever fit them, fit them on the train split only, which is why the
split boundaries are reported alongside every result.

**2. LLM contamination.** A language model's training data may already contain
knowledge of what followed a historical event, so a backtest scored by the model
is not a clean test of the model. `sentiment_mode="rule_based"` is the default
and uses no LLM at all. `sentiment_mode="llm"` is available, and everything it
produces is stamped `llm_contaminated=True` and labelled in the UI.

**3. Non-independence.** Holding periods that overlap in time are not
independent observations, and neither are events that cluster around one policy
episode. Overlap is measured and reported rather than left for the reader to
notice.
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
from ..embeddings.service import EmbeddingService
from ..market import calendar as mcal
from ..market.service import MarketDataService
from ..models import BacktestRun, Event, EventTicker, utcnow
from . import historical, relevance, signals

log = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252
#: Below this many observations a Sharpe ratio is noise dressed as a number.
MIN_SHARPE_SAMPLE = 20


@dataclass
class BacktestParams:
    start: dt.date
    end: dt.date
    ticker: str | None = None
    event_type: str | None = None
    min_signal: float = 0.2
    holding_days: int = 5
    sentiment_mode: str = "rule_based"  # rule_based | llm
    include_low_confidence: bool = False
    #: Fractions of the date range assigned to train / validation / test.
    splits: tuple[float, float, float] = (0.6, 0.2, 0.2)

    def as_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "ticker": self.ticker,
            "event_type": self.event_type,
            "min_signal": self.min_signal,
            "holding_days": self.holding_days,
            "sentiment_mode": self.sentiment_mode,
            "include_low_confidence": self.include_low_confidence,
            "splits": list(self.splits),
        }


@dataclass
class Observation:
    """One event/ticker pair that cleared the threshold."""

    event_id: str
    ticker: str
    event_type: str
    source_timestamp: dt.datetime
    reaction_day: dt.date
    exit_day: dt.date
    basis: str
    score: float
    direction: int  # +1 or -1
    sample_size: int
    sample_flag: str
    raw_return: float
    aligned_return: float  # raw return signed by the signal's direction
    split: str

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "ticker": self.ticker,
            "event_type": self.event_type,
            "source_timestamp": self.source_timestamp.isoformat(),
            "reaction_day": self.reaction_day.isoformat(),
            "exit_day": self.exit_day.isoformat(),
            "basis": self.basis,
            "score": round(self.score, 4),
            "direction": self.direction,
            "sample_size": self.sample_size,
            "sample_flag": self.sample_flag,
            "raw_return": round(self.raw_return, 6),
            "aligned_return": round(self.aligned_return, 6),
            "split": self.split,
        }


def max_drawdown(returns: list[float]) -> float:
    """Largest peak-to-trough fall of the cumulative sum, in return units.

    Computed on the *cumulative sum* of per-observation returns in chronological
    order. That is the right shape for a sequence of independent fixed-size
    observations; it is not a compounded equity curve, because there is no
    capital being compounded here.
    """
    if not returns:
        return 0.0
    cumulative = 0.0
    peak = 0.0
    worst = 0.0
    for value in returns:
        cumulative += value
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return worst


def sharpe_ratio(returns: list[float], holding_days: int) -> float | None:
    """Annualised mean/sd of the per-observation returns.

    Returns None below `MIN_SHARPE_SAMPLE`: a Sharpe computed on a handful of
    overlapping observations is not a measurement, and reporting one would
    invite exactly the over-reading this tool exists to avoid. The cash rate in
    the numerator is taken as zero and said so in the notes. (It is spelled
    "cash rate" rather than the textbook term throughout, because the textbook
    term contains a phrase this app never puts in front of a reader.)
    """
    n = len(returns)
    if n < MIN_SHARPE_SAMPLE:
        return None
    sd = statistics.stdev(returns)
    if sd <= 0:
        return None
    periods_per_year = TRADING_DAYS_PER_YEAR / max(holding_days, 1)
    return (statistics.fmean(returns) / sd) * math.sqrt(periods_per_year)


def overlap_fraction(observations: list[Observation]) -> float:
    """Share of observations whose holding window overlaps the previous one.

    High overlap means the observations are not independent, so every dispersion
    statistic below (sd, Sharpe, drawdown) is optimistic.
    """
    if len(observations) < 2:
        return 0.0
    ordered = sorted(observations, key=lambda o: o.reaction_day)
    overlapping = sum(
        1
        for previous, current in zip(ordered, ordered[1:])
        if current.reaction_day <= previous.exit_day
    )
    return overlapping / (len(ordered) - 1)


def summarise(observations: list[Observation], holding_days: int) -> dict:
    """The metric block the spec asks for, with honest absences."""
    returns = [o.aligned_return for o in sorted(observations, key=lambda o: o.reaction_day)]
    n = len(returns)
    block: dict = {
        "n": n,
        "flag": historical.sample_flag(n),
        "mean": None,
        "median": None,
        "stdev": None,
        "win_rate": None,
        "max_drawdown": None,
        "sharpe": None,
        "best": None,
        "worst": None,
        "long_count": sum(1 for o in observations if o.direction > 0),
        "short_count": sum(1 for o in observations if o.direction < 0),
        "overlap_fraction": round(overlap_fraction(observations), 3),
    }
    if n == 0:
        return block

    block["mean"] = round(statistics.fmean(returns), 6)
    block["median"] = round(statistics.median(returns), 6)
    block["stdev"] = round(statistics.stdev(returns), 6) if n > 1 else 0.0
    block["win_rate"] = round(100.0 * sum(1 for r in returns if r > 0) / n, 2)
    block["max_drawdown"] = round(max_drawdown(returns), 6)
    block["best"] = round(max(returns), 6)
    block["worst"] = round(min(returns), 6)

    sharpe = sharpe_ratio(returns, holding_days)
    block["sharpe"] = round(sharpe, 3) if sharpe is not None else None
    return block


def split_boundaries(params: BacktestParams) -> list[tuple[str, dt.date, dt.date]]:
    """Chronological train / validation / test windows.

    Chronological, never random: shuffling time-series observations into random
    folds lets the future inform the past, which is the same leak this module
    exists to prevent.
    """
    total_days = max((params.end - params.start).days, 1)
    train_frac, validation_frac, _ = params.splits
    train_end = params.start + dt.timedelta(days=int(total_days * train_frac))
    validation_end = params.start + dt.timedelta(
        days=int(total_days * (train_frac + validation_frac))
    )
    return [
        ("train", params.start, train_end),
        ("validation", train_end, validation_end),
        ("test", validation_end, params.end),
    ]


def assign_split(day: dt.date, boundaries: list[tuple[str, dt.date, dt.date]]) -> str:
    for name, start, end in boundaries:
        if start <= day <= end:
            return name
    return "test"


def _candidate_events(db: Session, params: BacktestParams) -> list[Event]:
    start = dt.datetime.combine(params.start, dt.time.min, tzinfo=dt.timezone.utc)
    end = dt.datetime.combine(params.end, dt.time.max, tzinfo=dt.timezone.utc)

    stmt = (
        select(Event)
        .where(
            Event.source_timestamp >= start,
            Event.source_timestamp <= end,
            Event.relevant.is_(True),
        )
        .order_by(Event.source_timestamp)
    )
    if params.event_type:
        stmt = stmt.where(Event.event_type == params.event_type)
    if params.ticker:
        stmt = stmt.join(EventTicker, EventTicker.event_id == Event.id).where(
            EventTicker.ticker == params.ticker.upper()
        )
    return list(db.execute(stmt).scalars().unique())


def point_in_time_signal(
    db: Session,
    event: Event,
    ticker: str,
    *,
    market: MarketDataService,
    embeddings: EmbeddingService | None,
    sentiment_mode: str,
    ticker_confidence: str,
) -> signals.SignalResult | None:
    """Recompute the signal as it would have looked at the event's timestamp.

    The stored signal is not reused: under `rule_based` mode the sentiment has to
    come from the lexicon rather than from whatever the model said, and
    recomputing keeps both modes on exactly the same code path.
    """
    horizon = settings.signal_primary_horizon
    available = market.available_horizons()
    if horizon not in available:
        horizon = available[-1] if available else "1d"

    sentiment_model: str | None = None
    if sentiment_mode == "llm":
        analysis = event.analysis
        if analysis is None or analysis.status != "COMPLETE" or analysis.sentiment is None:
            return None  # no model reading exists for this event; do not invent one
        sentiment = float(analysis.sentiment)
        confidence = float(analysis.confidence or 0.5)
        source = "model"
        sentiment_model = analysis.model
    else:
        sentiment = relevance.rule_based_sentiment(event.text, event.title)
        confidence = 0.35
        source = "rule_based"

    comparable = historical.build_comparable_set(
        db,
        market,
        event=event,
        ticker=ticker,
        horizons=[horizon],
        persist=False,          # a backtest must not write into live tables
        embeddings=embeddings,
    )
    novelty = historical.novelty(db, event, embeddings=embeddings)

    return signals.compute_signal(
        sentiment=sentiment,
        sentiment_source=source,
        model_confidence=confidence,
        stats=comparable.stats.get(horizon),
        novelty_value=novelty.value,
        max_similarity=novelty.max_similarity,
        horizon=horizon,
        ticker_confidence=ticker_confidence,
        sentiment_model=sentiment_model,
    )


@dataclass
class BacktestResult:
    params: BacktestParams
    overall: dict = field(default_factory=dict)
    by_split: dict = field(default_factory=dict)
    observations: list[Observation] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    llm_contaminated: bool = False
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "params": self.params.as_dict(),
            "overall": self.overall,
            "by_split": self.by_split,
            "observations": [o.as_dict() for o in self.observations],
            "skipped_count": len(self.skipped),
            "skipped": self.skipped[:50],
            "llm_contaminated": self.llm_contaminated,
            "notes": self.notes,
            "warnings": self.warnings,
        }


def run_backtest(
    db: Session,
    params: BacktestParams,
    *,
    market: MarketDataService | None = None,
    embeddings: EmbeddingService | None = None,
    max_events: int = 2000,
) -> BacktestResult:
    """Run a backtest. Never raises on a single bad event; it is skipped and listed."""
    market = market or MarketDataService(db)
    if embeddings is None:
        try:
            embeddings = EmbeddingService(db)
        except Exception as exc:  # similarity is an enhancement, not a requirement
            log.warning("backtest running without embeddings: %s", exc)

    result = BacktestResult(
        params=params, llm_contaminated=(params.sentiment_mode == "llm")
    )
    boundaries = split_boundaries(params)
    events = _candidate_events(db, params)[:max_events]

    for event in events:
        links = [
            link
            for link in event.tickers
            if params.ticker is None or link.ticker == params.ticker.upper()
        ]
        if not params.include_low_confidence:
            links = [link for link in links if link.confidence in ("HIGH", "MEDIUM")]

        for link in links:
            try:
                signal = point_in_time_signal(
                    db,
                    event,
                    link.ticker,
                    market=market,
                    embeddings=embeddings,
                    sentiment_mode=params.sentiment_mode,
                    ticker_confidence=link.confidence,
                )
            except Exception as exc:
                result.skipped.append(
                    {"event_id": event.id, "ticker": link.ticker, "reason": str(exc)[:200]}
                )
                continue

            if signal is None:
                result.skipped.append(
                    {
                        "event_id": event.id,
                        "ticker": link.ticker,
                        "reason": "no model analysis available for llm sentiment mode",
                    }
                )
                continue
            if abs(signal.score) < params.min_signal:
                continue

            outcome = _holding_period_return(
                market, link.ticker, event.source_timestamp, params.holding_days
            )
            if outcome is None:
                result.skipped.append(
                    {
                        "event_id": event.id,
                        "ticker": link.ticker,
                        "reason": "no price data covering the holding period",
                    }
                )
                continue

            reaction_day, exit_day, basis, raw_return = outcome
            direction = 1 if signal.score > 0 else -1
            result.observations.append(
                Observation(
                    event_id=event.id,
                    ticker=link.ticker,
                    event_type=event.event_type,
                    source_timestamp=event.source_timestamp,
                    reaction_day=reaction_day,
                    exit_day=exit_day,
                    basis=basis,
                    score=signal.score,
                    direction=direction,
                    sample_size=signal.sample_size,
                    sample_flag=signal.sample_flag,
                    raw_return=raw_return,
                    aligned_return=raw_return * direction,
                    split=assign_split(reaction_day, boundaries),
                )
            )

    result.overall = summarise(result.observations, params.holding_days)
    for name, start, end in boundaries:
        subset = [o for o in result.observations if o.split == name]
        result.by_split[name] = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            **summarise(subset, params.holding_days),
        }

    _add_notes(result, params)
    return result


def _holding_period_return(
    market: MarketDataService,
    ticker: str,
    event_time: dt.datetime,
    holding_days: int,
) -> tuple[dt.date, dt.date, str, float] | None:
    """Return over `holding_days` sessions from the pre-event baseline.

    Same convention as the live statistics: the baseline is the close before the
    first session that could react, so the reaction day's own move is included.
    """
    anchor_ts, basis = mcal.anchor(event_time)
    reaction_day = anchor_ts.astimezone(mcal.EASTERN).date()
    base_day = mcal.previous_trading_day(reaction_day)
    exit_day = (
        reaction_day
        if holding_days <= 1
        else mcal.trading_days_after(reaction_day, holding_days - 1)
    )

    market.ensure_daily(
        ticker,
        base_day - dt.timedelta(days=10),
        exit_day + dt.timedelta(days=10),
    )
    closes = market.daily_closes(ticker, base_day, exit_day)
    base = closes.get(base_day)
    exit_price = closes.get(exit_day)
    if base is None or exit_price is None or base <= 0:
        return None
    return reaction_day, exit_day, basis, (exit_price / base) - 1.0


def _add_notes(result: BacktestResult, params: BacktestParams) -> None:
    if params.sentiment_mode == "llm":
        result.warnings.append(
            "POTENTIALLY CONTAMINATED: sentiment came from a language model whose "
            "training data may already contain knowledge of what followed these "
            "events. Treat this run as an upper bound, not a measurement."
        )
    else:
        result.notes.append(
            "Sentiment came from the rule-based lexicon. No language model was "
            "involved, so there is no training-data contamination."
        )

    n = result.overall.get("n", 0)
    if n < settings.min_usable_sample:
        result.warnings.append(
            f"Only {n} observations cleared the threshold. Nothing here is a "
            "measurement of anything."
        )
    if n > 0:
        result.notes.append(
            f"Sharpe is annualised, assumes a zero cash rate, and is withheld below "
            f"{MIN_SHARPE_SAMPLE} observations."
        )
    overlap = result.overall.get("overlap_fraction") or 0.0
    if overlap > 0.25:
        result.warnings.append(
            f"{overlap:.0%} of holding windows overlap the previous observation. "
            "Overlapping windows are not independent, so the standard deviation, "
            "Sharpe and drawdown are all flattered."
        )
    unreliable = sum(1 for o in result.observations if o.sample_flag == "unreliable")
    if unreliable:
        result.notes.append(
            f"{unreliable} of {len(result.observations)} signals were built on an "
            "unreliable historical sample (N < 10), where the historical components "
            "carry zero weight."
        )
    result.notes.append(
        "No costs, spread, slippage, position sizing or capital are modelled. "
        "These are signal-aligned price changes, not returns on a portfolio."
    )
    result.notes.append(
        "Every signal was recomputed point-in-time: comparable events, similarity "
        "and novelty were all bounded by each event's own timestamp."
    )


def store_run(db: Session, user_id: str, result: BacktestResult) -> BacktestRun:
    run = BacktestRun(
        user_id=user_id,
        params=result.params.as_dict(),
        results={
            "overall": result.overall,
            "by_split": result.by_split,
            "notes": result.notes,
            "warnings": result.warnings,
            "skipped_count": len(result.skipped),
            "observation_count": len(result.observations),
        },
        sentiment_mode=result.params.sentiment_mode,
        llm_contaminated=result.llm_contaminated,
        status="COMPLETE",
        completed_at=utcnow(),
    )
    db.add(run)
    db.commit()
    return run
