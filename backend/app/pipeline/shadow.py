"""Run a second model alongside the primary one, for comparison only.

The question this answers: *is the cheap model good enough?* Running Haiku for
everything is a fifth of the cost of Opus, and the honest way to find out what
that costs in quality is to run both on the same events and look at where they
disagree -- not to reason about it.

**It can never affect a signal, an alert or a backtest**, and that is enforced
structurally rather than by care:

* results go to `shadow_analyses`, a separate table;
* nothing in `signals.py`, `historical.py`, `notifications.py` or `backtest.py`
  imports that table or this module;
* there is no relationship from `Event` to it, so `event.analysis` cannot
  resolve to a shadow row even by accident.

A flag on `claude_analyses` would have been less code and one forgotten filter
away from a shadow reading driving a real alert.

**Off by default.** It is a second bill: `SHADOW_ANALYSIS_MODEL` unset means not
a single extra call. When set, only `SHADOW_SAMPLE_RATE` of analysed events are
duplicated (default 0.1), and the sampling is deterministic per event so a
re-run does not quietly resample and double the cost.
"""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..llm.client import AnthropicClient
from ..models import Event, ShadowAnalysis

log = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(settings.shadow_analysis_model) and settings.shadow_sample_rate > 0


def should_sample(event: Event, rate: float | None = None) -> bool:
    """Deterministic per-event sampling.

    A random draw would resample on every pipeline re-run, so a re-processed
    backlog would be charged for shadow analyses over and over. Hashing the
    event id gives a stable, uniformly distributed decision: the same event is
    always either in the sample or not.
    """
    sample_rate = settings.shadow_sample_rate if rate is None else rate
    if sample_rate <= 0:
        return False
    if sample_rate >= 1:
        return True
    digest = hashlib.sha256(f"shadow:{event.id}".encode()).digest()
    # First 8 bytes as a fraction of the range -- stable across processes and
    # Python versions, unlike hash().
    bucket = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return bucket < sample_rate


def existing(db: Session, content_hash: str, model: str) -> ShadowAnalysis | None:
    return (
        db.execute(
            select(ShadowAnalysis).where(
                ShadowAnalysis.content_hash == content_hash,
                ShadowAnalysis.model == model,
            )
        )
        .scalars()
        .first()
    )


def run_shadow_analysis(
    db: Session, event: Event, client: AnthropicClient | None = None
) -> ShadowAnalysis | None:
    """Analyse `event` with the shadow model, if it is enabled and sampled.

    Returns None when nothing was done. Never raises: a shadow failure is not
    allowed to affect the pipeline it is riding along with -- the comparison is
    a nice-to-have and the analysis it shadows is not.
    """
    if not enabled():
        return None

    model = settings.shadow_analysis_model
    if not should_sample(event):
        return None

    cached = existing(db, event.content_hash, model)
    if cached is not None:
        if cached.event_id is None:
            cached.event_id = event.id
        return cached

    client = client or AnthropicClient()
    if not client.available:
        return None

    try:
        outcome = client.analyse(
            source=event.source_key,
            author=event.author,
            timestamp=event.source_timestamp.isoformat(),
            title=event.title,
            text=event.text,
            model=model,
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("shadow analysis of %s failed: %s", event.id, exc)
        return None

    parsed = outcome.parsed or {}
    row = ShadowAnalysis(
        event_id=event.id,
        content_hash=event.content_hash,
        model=outcome.model or model,
        status=outcome.status,
        raw_response=outcome.raw_response,
        parsed=outcome.parsed or None,
        validation_error=outcome.error,
        event_type=parsed.get("event_type"),
        sentiment=parsed.get("sentiment"),
        market_impact=parsed.get("market_impact"),
        confidence=parsed.get("confidence"),
        time_horizon=parsed.get("time_horizon"),
        estimated_cost_usd=round(outcome.cost, 6),
    )
    db.add(row)
    db.flush()
    return row


def comparison(db: Session, limit: int = 200) -> dict:
    """Primary vs shadow, on the events that have both.

    Deliberately simple: counts, mean absolute sentiment difference, how often
    the two disagree on *direction* (the thing that would change a signal), and
    what the shadow model has cost. Direction disagreement is the number worth
    looking at -- two models differing by 0.1 on sentiment changes nothing,
    while one saying bullish and the other bearish changes everything.
    """
    from ..models import ClaudeAnalysis

    rows = list(
        db.execute(
            select(ClaudeAnalysis, ShadowAnalysis)
            .join(ShadowAnalysis, ShadowAnalysis.content_hash == ClaudeAnalysis.content_hash)
            .where(
                ClaudeAnalysis.status == "COMPLETE",
                ShadowAnalysis.status == "COMPLETE",
            )
            .limit(limit)
        )
    )

    pairs = [
        (primary, shadow)
        for primary, shadow in rows
        if primary.sentiment is not None and shadow.sentiment is not None
    ]

    total_cost = float(
        db.execute(
            select(func.coalesce(func.sum(ShadowAnalysis.estimated_cost_usd), 0.0))
        ).scalar()
        or 0.0
    )
    shadow_rows = int(
        db.execute(select(func.count()).select_from(ShadowAnalysis)).scalar() or 0
    )

    if not pairs:
        return {
            "enabled": enabled(),
            "model": settings.shadow_analysis_model,
            "sample_rate": settings.shadow_sample_rate,
            "compared": 0,
            "shadow_rows": shadow_rows,
            "mean_abs_sentiment_diff": None,
            "direction_disagreement_pct": None,
            "mean_abs_confidence_diff": None,
            "event_type_disagreement_pct": None,
            "estimated_cost_usd": round(total_cost, 4),
            "examples": [],
        }

    def direction(value: float) -> int:
        if value > 0.05:
            return 1
        if value < -0.05:
            return -1
        return 0

    sentiment_diffs = [abs(p.sentiment - s.sentiment) for p, s in pairs]
    confidence_diffs = [
        abs((p.confidence or 0.0) - (s.confidence or 0.0)) for p, s in pairs
    ]
    direction_clashes = sum(
        1 for p, s in pairs if direction(p.sentiment) != direction(s.sentiment)
    )
    type_clashes = sum(1 for p, s in pairs if (p.event_type or "") != (s.event_type or ""))

    # The biggest disagreements, which is what you would actually want to read.
    worst = sorted(pairs, key=lambda pair: abs(pair[0].sentiment - pair[1].sentiment), reverse=True)

    return {
        "enabled": enabled(),
        "model": settings.shadow_analysis_model,
        "sample_rate": settings.shadow_sample_rate,
        "compared": len(pairs),
        "shadow_rows": shadow_rows,
        "primary_model": pairs[0][0].model,
        "mean_abs_sentiment_diff": round(sum(sentiment_diffs) / len(sentiment_diffs), 4),
        "mean_abs_confidence_diff": round(sum(confidence_diffs) / len(confidence_diffs), 4),
        "direction_disagreement_pct": round(100.0 * direction_clashes / len(pairs), 1),
        "event_type_disagreement_pct": round(100.0 * type_clashes / len(pairs), 1),
        "estimated_cost_usd": round(total_cost, 4),
        "examples": [
            {
                "event_id": p.event_id,
                "primary_sentiment": p.sentiment,
                "shadow_sentiment": s.sentiment,
                "primary_event_type": p.event_type,
                "shadow_event_type": s.event_type,
            }
            for p, s in worst[:5]
        ],
    }
