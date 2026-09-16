"""LLM analysis orchestration: cache, budget, usage accounting.

Cost control, in the order the spec asks for it:
  1. dedup happens upstream -- identical content never gets here twice;
  2. the rule filter in `relevance.py` gates what reaches the model at all;
  3. `claude_analyses` is a **permanent cache keyed by content hash**, so the
     same text is never analysed twice even across events and sources;
  4. a daily call budget caps the worst case;
  5. every call writes an `llm_usage` row with tokens and an estimated cost.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..llm.client import AnthropicClient, LLMOutcome
from ..models import ClaudeAnalysis, Event, LLMUsage, utcnow

log = logging.getLogger(__name__)


def calls_today(db: Session) -> int:
    since = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        db.execute(select(func.count(LLMUsage.id)).where(LLMUsage.created_at >= since)).scalar() or 0
    )


def budget_available(db: Session) -> bool:
    return calls_today(db) < settings.llm_daily_call_budget


def record_usage(db: Session, *, purpose: str, event_id: str | None, outcome: LLMOutcome) -> None:
    db.add(
        LLMUsage(
            purpose=purpose,
            model=outcome.model,
            event_id=event_id,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            cache_read_tokens=outcome.cache_read_tokens,
            estimated_cost_usd=round(outcome.cost, 6),
            ok=outcome.ok,
            error=(outcome.error or None),
        )
    )
    db.flush()


def cached_analysis(db: Session, content_hash: str) -> ClaudeAnalysis | None:
    return (
        db.execute(select(ClaudeAnalysis).where(ClaudeAnalysis.content_hash == content_hash))
        .scalars()
        .first()
    )


def analyse_event(
    db: Session, event: Event, client: AnthropicClient | None = None
) -> ClaudeAnalysis:
    """Analyse one event, or reuse the cached analysis for identical content.

    Always returns a row; the row's `status` carries PENDING / COMPLETE / FAILED
    / UNAVAILABLE, and the event's `analysis_status` mirrors it. A model failure
    never stops the pipeline.
    """
    cached = cached_analysis(db, event.content_hash)
    if cached is not None and cached.status == "COMPLETE":
        # Reuse: attach to this event without spending a token.
        if cached.event_id is None:
            cached.event_id = event.id
        _apply_to_event(event, cached)
        return cached

    client = client or AnthropicClient()
    event.analysis_attempts += 1

    if not client.available:
        return _store_outcome(
            db,
            event,
            LLMOutcome(status="UNAVAILABLE", error="no Anthropic API key configured",
                       model=settings.anthropic_analysis_model),
            record=False,
        )

    if not budget_available(db):
        log.warning("daily LLM call budget exhausted; deferring analysis of %s", event.id)
        return _store_outcome(
            db,
            event,
            LLMOutcome(
                status="UNAVAILABLE",
                error=f"daily LLM call budget of {settings.llm_daily_call_budget} reached",
                model=settings.anthropic_analysis_model,
            ),
            record=False,
            status_override="PENDING",
        )

    outcome = client.analyse(
        source=event.source_key,
        author=event.author,
        timestamp=event.source_timestamp.isoformat(),
        title=event.title,
        text=event.text,
    )
    record_usage(db, purpose="analysis", event_id=event.id, outcome=outcome)
    return _store_outcome(db, event, outcome)


def triage_event(db: Session, event: Event, client: AnthropicClient | None = None) -> bool:
    """Cheap-model triage. Returns whether the event should get a full analysis.

    If triage is unavailable (no key) or fails, we fall back to the rule-based
    verdict already stored on the event -- the pipeline must work without keys.
    """
    client = client or AnthropicClient()
    if not client.available or not budget_available(db):
        return event.relevant

    outcome = client.triage(source=event.source_key, title=event.title, text=event.text)
    record_usage(db, purpose="triage", event_id=event.id, outcome=outcome)
    if not outcome.ok or not outcome.parsed:
        return event.relevant

    relevant = bool(outcome.parsed.get("relevant"))
    score = float(outcome.parsed.get("score") or 0.0)
    reason = str(outcome.parsed.get("reason") or "")
    # Keep the higher of the two scores; the rule filter is permissive by design
    # and we only want triage to *add* confidence, not silently veto everything.
    if score > event.relevance_score:
        event.relevance_score = score
    event.relevance_reason = f"{event.relevance_reason or ''} | triage: {reason}".strip(" |")
    event.relevant = event.relevant or relevant
    return event.relevant


def _apply_to_event(event: Event, analysis: ClaudeAnalysis) -> None:
    event.analysis_status = analysis.status
    if analysis.status == "COMPLETE" and analysis.event_type:
        event.event_type = analysis.event_type


def _store_outcome(
    db: Session,
    event: Event,
    outcome: LLMOutcome,
    *,
    record: bool = True,
    status_override: str | None = None,
) -> ClaudeAnalysis:
    parsed = outcome.parsed or None
    existing = (
        db.execute(select(ClaudeAnalysis).where(ClaudeAnalysis.event_id == event.id))
        .scalars()
        .first()
    )
    row = existing or cached_analysis(db, event.content_hash)

    if row is None:
        row = ClaudeAnalysis(event_id=event.id, content_hash=event.content_hash)
        db.add(row)

    row.model = outcome.model
    row.status = outcome.status
    row.raw_response = outcome.raw_response
    row.parsed = parsed
    row.validation_error = outcome.error
    row.attempts = max(row.attempts or 0, outcome.attempts)
    row.created_at = row.created_at or utcnow()
    if parsed:
        row.event_type = parsed.get("event_type")
        row.sentiment = parsed.get("sentiment")
        row.market_impact = parsed.get("market_impact")
        row.confidence = parsed.get("confidence")
        row.time_horizon = parsed.get("time_horizon")

    event.analysis_status = status_override or outcome.status
    if outcome.ok and row.event_type:
        event.event_type = row.event_type
    db.flush()
    return row


def retryable_events(db: Session, *, max_attempts: int = 3, limit: int = 20) -> list[Event]:
    """Events whose analysis failed and is still worth another attempt."""
    stmt = (
        select(Event)
        .where(
            Event.relevant.is_(True),
            Event.analysis_status.in_(["FAILED", "PENDING"]),
            Event.analysis_attempts < max_attempts,
            Event.source_timestamp >= utcnow() - dt.timedelta(days=3),
        )
        .order_by(Event.source_timestamp.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars())
