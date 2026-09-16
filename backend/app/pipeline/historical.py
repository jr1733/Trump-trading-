"""Historical response: find comparable past events and summarise what followed.

Comparability is `same event_type` AND (`same ticker` OR `same sector`). Phase 2
annotates each comparable event with its embedding cosine similarity, but does
NOT let similarity define the sample: a score that could silently add or drop
events would make the sample size -- the number the whole signal is gated on --
depend on a tunable threshold. Similarity is reporting; event type and ticker
are the sample.

**Look-ahead prevention** is enforced here, not left to the caller: every query
is bounded by `before` (the subject event's timestamp), so a historical statistic
can never include an event that had not yet happened. `find_comparable_events`
takes `before` as a required argument for exactly that reason.
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..market.service import MarketDataService
from ..models import Event, EventTicker, HistoricalEventMatch, Ticker

log = logging.getLogger(__name__)


@dataclass
class HorizonStats:
    horizon: str
    n: int
    mean: float | None = None
    median: float | None = None
    stdev: float | None = None
    positive_pct: float | None = None
    negative_pct: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    p5: float | None = None
    p95: float | None = None
    mean_abnormal: float | None = None
    median_abnormal: float | None = None
    flag: str = "unreliable"  # unreliable | limited | ok
    basis: str = "daily"

    def as_dict(self) -> dict:
        return {
            "horizon": self.horizon,
            "n": self.n,
            "mean": self.mean,
            "median": self.median,
            "stdev": self.stdev,
            "positive_pct": self.positive_pct,
            "negative_pct": self.negative_pct,
            "min": self.minimum,
            "max": self.maximum,
            "p5": self.p5,
            "p95": self.p95,
            "mean_abnormal": self.mean_abnormal,
            "median_abnormal": self.median_abnormal,
            "flag": self.flag,
            "basis": self.basis,
        }


@dataclass
class ComparableSet:
    ticker: str
    event_type: str
    match_basis: str
    matches: list[dict] = field(default_factory=list)
    stats: dict[str, HorizonStats] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "event_type": self.event_type,
            "match_basis": self.match_basis,
            "n_matches": len(self.matches),
            "matches": self.matches,
            "stats": {k: v.as_dict() for k, v in self.stats.items()},
        }


def sample_flag(n: int) -> str:
    """The spec's sample-size gates, in one place."""
    if n < settings.min_usable_sample:
        return "unreliable"
    if n < settings.min_reliable_sample:
        return "limited"
    return "ok"


def percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile. `pct` in [0, 100]."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * pct / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarise(horizon: str, raw: list[float], abnormal: list[float], basis: str) -> HorizonStats:
    n = len(raw)
    stats = HorizonStats(horizon=horizon, n=n, flag=sample_flag(n), basis=basis)
    if n == 0:
        return stats
    stats.mean = round(statistics.fmean(raw), 6)
    stats.median = round(statistics.median(raw), 6)
    stats.stdev = round(statistics.stdev(raw), 6) if n > 1 else 0.0
    stats.positive_pct = round(100.0 * sum(1 for r in raw if r > 0) / n, 2)
    stats.negative_pct = round(100.0 * sum(1 for r in raw if r < 0) / n, 2)
    stats.minimum = round(min(raw), 6)
    stats.maximum = round(max(raw), 6)
    p5, p95 = percentile(raw, 5), percentile(raw, 95)
    stats.p5 = round(p5, 6) if p5 is not None else None
    stats.p95 = round(p95, 6) if p95 is not None else None
    if abnormal:
        stats.mean_abnormal = round(statistics.fmean(abnormal), 6)
        stats.median_abnormal = round(statistics.median(abnormal), 6)
    return stats


def find_comparable_events(
    db: Session,
    *,
    event_type: str,
    ticker: str,
    before: dt.datetime,
    exclude_event_id: str | None = None,
    limit: int = 200,
    include_sector: bool = True,
) -> tuple[list[Event], str]:
    """Past events of the same type touching this ticker (or its sector).

    `before` is mandatory and strictly enforced: nothing at or after the subject
    event's timestamp can enter the sample.
    """
    stmt = (
        select(Event)
        .join(EventTicker, EventTicker.event_id == Event.id)
        .where(
            Event.event_type == event_type,
            Event.source_timestamp < before,
            EventTicker.ticker == ticker.upper(),
            # LOW-confidence ticker matches are excluded from statistics.
            EventTicker.confidence.in_(["HIGH", "MEDIUM"]),
        )
        .order_by(Event.source_timestamp.desc())
        .limit(limit)
    )
    if exclude_event_id:
        stmt = stmt.where(Event.id != exclude_event_id)
    events = list(db.execute(stmt).scalars().unique())
    if events or not include_sector:
        return events, "event_type+ticker"

    # Fall back to the sector: same event type, any ticker sharing this sector ETF.
    row = db.get(Ticker, ticker.upper())
    if row is None or not row.sector_etf:
        return [], "event_type+ticker"

    peers = list(
        db.execute(select(Ticker.symbol).where(Ticker.sector_etf == row.sector_etf)).scalars()
    )
    if not peers:
        return [], "event_type+ticker"

    stmt = (
        select(Event)
        .join(EventTicker, EventTicker.event_id == Event.id)
        .where(
            Event.event_type == event_type,
            Event.source_timestamp < before,
            EventTicker.ticker.in_(peers),
            EventTicker.confidence.in_(["HIGH", "MEDIUM"]),
        )
        .order_by(Event.source_timestamp.desc())
        .limit(limit)
    )
    if exclude_event_id:
        stmt = stmt.where(Event.id != exclude_event_id)
    return list(db.execute(stmt).scalars().unique()), "event_type+sector"


def build_comparable_set(
    db: Session,
    market: MarketDataService,
    *,
    event: Event,
    ticker: str,
    horizons: list[str] | None = None,
    persist: bool = True,
    embeddings=None,
) -> ComparableSet:
    """Historical response for one (event, ticker) pair.

    When an embedding service is supplied, each comparable event also carries
    its cosine similarity to the subject. That is reporting only -- the sample
    is still defined by event type and ticker/sector, so a similarity score
    cannot quietly widen or narrow the statistical sample.
    """
    horizons = horizons or market.available_horizons()
    past, basis = find_comparable_events(
        db,
        event_type=event.event_type,
        ticker=ticker,
        before=event.source_timestamp,
        exclude_event_id=event.id,
    )

    similarity_by_event: dict[str, float] = {}
    if embeddings is not None and past:
        try:
            similarity_by_event = {
                match.event.id: match.similarity
                for match in embeddings.similar_events(
                    event, limit=len(past) * 2 + 10, threshold=-1.0
                )
            }
        except Exception as exc:
            log.warning("similarity annotation failed: %s", exc)

    result = ComparableSet(ticker=ticker.upper(), event_type=event.event_type, match_basis=basis)
    per_horizon_raw: dict[str, list[float]] = {h: [] for h in horizons}
    per_horizon_abn: dict[str, list[float]] = {h: [] for h in horizons}
    data_basis = "intraday" if any(h.endswith("m") for h in horizons) else "daily"

    for match in past:
        returns = market.compute_returns(ticker, match.source_timestamp, horizons)
        if not returns:
            continue
        serialised = {h: r.as_dict() for h, r in returns.items()}
        for horizon, item in returns.items():
            per_horizon_raw[horizon].append(item.raw_return)
            if item.abnormal_return is not None:
                per_horizon_abn[horizon].append(item.abnormal_return)
        result.matches.append(
            {
                "event_id": match.id,
                "title": match.title or (match.text[:120] if match.text else ""),
                "source": match.source_key,
                "source_timestamp": match.source_timestamp.isoformat(),
                "similarity": similarity_by_event.get(match.id),
                "returns": serialised,
            }
        )
        if persist:
            _persist_match(
                db, event, match, ticker, serialised, similarity_by_event.get(match.id)
            )

    for horizon in horizons:
        result.stats[horizon] = summarise(
            horizon, per_horizon_raw[horizon], per_horizon_abn[horizon], data_basis
        )
    return result


def _persist_match(
    db: Session,
    event: Event,
    match: Event,
    ticker: str,
    returns: dict,
    similarity: float | None = None,
) -> None:
    existing = (
        db.execute(
            select(HistoricalEventMatch).where(
                HistoricalEventMatch.event_id == event.id,
                HistoricalEventMatch.matched_event_id == match.id,
                HistoricalEventMatch.ticker == ticker.upper(),
            )
        )
        .scalars()
        .first()
    )
    if existing:
        existing.returns = returns
        if similarity is not None:
            existing.similarity = similarity
        return
    db.add(
        HistoricalEventMatch(
            event_id=event.id,
            matched_event_id=match.id,
            ticker=ticker.upper(),
            similarity=similarity or 0.0,
            match_basis="event_type+ticker",
            returns=returns,
        )
    )


@dataclass
class Novelty:
    value: float           # 1 - max_similarity, in [0, 1]
    max_similarity: float
    measure: str           # which similarity function produced it
    matched_event_id: str | None = None


def novelty(
    db: Session,
    event: Event,
    *,
    window_days: int = 30,
    embeddings=None,
) -> Novelty:
    """1 - max similarity to events in the trailing `window_days`.

    Uses embeddings when a vector exists for this event, and falls back to the
    Phase 1 lexical token-set (Jaccard) measure otherwise -- a brand-new event
    that the embedding worker has not reached yet must still get a signal. The
    `measure` field names whichever ran, and the "Why?" panel shows it, so a
    lexical score is never presented as a semantic one.
    """
    if embeddings is not None:
        try:
            similarity, matched_id = embeddings.max_similarity(event, window_days=window_days)
            return Novelty(
                value=round(1.0 - similarity, 4),
                max_similarity=round(similarity, 4),
                measure=embeddings.provider.label,
                matched_event_id=matched_id,
            )
        except Exception as exc:
            # A vector search failure must not cost us the signal.
            log.warning("embedding novelty failed, falling back to lexical: %s", exc)

    from .ticker_match import jaccard, tokenize

    window_start = event.source_timestamp - dt.timedelta(days=window_days)
    stmt = (
        select(Event)
        .where(
            Event.source_timestamp >= window_start,
            Event.source_timestamp < event.source_timestamp,
            Event.id != event.id,
        )
        .order_by(Event.source_timestamp.desc())
        .limit(300)
    )
    recent = list(db.execute(stmt).scalars())
    if not recent:
        return Novelty(1.0, 0.0, "lexical token-set (Jaccard) -- no comparison set")

    subject = tokenize(f"{event.title or ''} {event.text}")
    best_score, best_id = 0.0, None
    for other in recent:
        score = jaccard(subject, tokenize(f"{other.title or ''} {other.text}"))
        if score > best_score:
            best_score, best_id = score, other.id
    return Novelty(
        value=round(1.0 - best_score, 4),
        max_similarity=round(best_score, 4),
        measure="lexical token-set (Jaccard) -- word overlap, not meaning",
        matched_event_id=best_id,
    )
