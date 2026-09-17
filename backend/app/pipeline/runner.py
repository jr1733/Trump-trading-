"""The pipeline.

    SOURCE -> NORMALISE -> DEDUPLICATE -> RELEVANCE (rules, then cheap model)
    -> ENTITY EXTRACTION -> TICKER MATCHING -> CLAUDE ANALYSIS -> STORE (stable id)
    -> HISTORICAL RESPONSE -> SIGNAL -> EVALUATE NOTIFICATION RULES -> DELIVER

**Idempotency is the load-bearing property here.** Every stage is safe to re-run:

* raw events are unique on ``(source_key, external_id)``;
* events are unique on ``content_hash`` (cross-source dedup);
* analyses are unique on ``content_hash`` (and cached permanently);
* signals are unique on ``(event_id, ticker)`` and updated in place;
* notifications are unique on their idempotency key.

Re-running the worker over the same data therefore produces no duplicate event,
signal or notification -- which is what the end-to-end test asserts.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..config import settings
from ..embeddings.service import EmbeddingService
from ..llm.client import AnthropicClient
from ..market.service import MarketDataService
from ..models import (
    Event,
    JobRun,
    Notification,
    RawEvent,
    Signal,
    SourceHealth,
    User,
    utcnow,
)
from . import analysis as analysis_mod
from . import historical, notifications, relevance, signals, ticker_match
from .push import build_push_provider

log = logging.getLogger(__name__)


@dataclass
class PipelineReport:
    raw_considered: int = 0
    events_created: int = 0
    events_duplicate: int = 0
    events_irrelevant: int = 0
    analyses_run: int = 0
    signals_written: int = 0
    notifications_created: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "raw_considered": self.raw_considered,
            "events_created": self.events_created,
            "events_duplicate": self.events_duplicate,
            "events_irrelevant": self.events_irrelevant,
            "analyses_run": self.analyses_run,
            "signals_written": self.signals_written,
            "notifications_created": self.notifications_created,
            "errors": self.errors,
        }


def primary_user(db: Session) -> User:
    user = db.execute(select(User).order_by(User.created_at)).scalars().first()
    if user is None:
        user = User(display_name="Operator")
        db.add(user)
        db.flush()
    return user


# --------------------------------------------------------------------------
# NORMALISE + DEDUPLICATE
# --------------------------------------------------------------------------
def normalise(raw: RawEvent) -> dict:
    """RawEvent -> the column values for an `events` row."""
    payload = raw.payload or {}
    return {
        "raw_event_id": raw.id,
        "source_key": raw.source_key,
        "external_id": raw.external_id,
        "author": payload.get("author") or _default_author(raw.source_key),
        "title": payload.get("title"),
        "text": payload.get("text") or payload.get("abstract") or payload.get("title") or "",
        "url": payload.get("url") or payload.get("html_url") or payload.get("link"),
        "media_url": payload.get("media_url"),
        "content_hash": raw.content_hash,
        "source_timestamp": raw.source_timestamp,
        "ingestion_timestamp": raw.ingestion_timestamp,
        # Backfilled archive rows are sample data for the historical statistics.
        # They must never raise an alert about something that happened in 2024.
        "is_historical": bool(payload.get("historical", False)),
    }


def _default_author(source_key: str) -> str:
    return {
        "whitehouse": "The White House",
        "federal_register": "Federal Register",
        "truth_social": "Donald J. Trump",
    }.get(source_key, source_key)


def upsert_event(db: Session, values: dict) -> tuple[Event | None, bool]:
    """Insert an event unless its content hash already exists.

    Returns `(event, created)`. `created=False` means this was a duplicate --
    the same press release republished, or the same story from two sources.
    """
    stmt = (
        insert(Event)
        .values(**values, processing_timestamp=utcnow())
        .on_conflict_do_nothing(constraint="uq_events_content_hash")
        .returning(Event.id)
    )
    new_id = db.execute(stmt).scalars().first()
    db.flush()
    if new_id is not None:
        return db.get(Event, new_id), True
    existing = (
        db.execute(select(Event).where(Event.content_hash == values["content_hash"]))
        .scalars()
        .first()
    )
    return existing, False


# --------------------------------------------------------------------------
# SIGNALS
# --------------------------------------------------------------------------
def build_signals_for_event(
    db: Session,
    event: Event,
    market: MarketDataService | None = None,
    *,
    include_low_confidence: bool = False,
    embeddings: EmbeddingService | None = None,
) -> list[Signal]:
    """Historical response + signal for each ticker on an event. Idempotent."""
    market = market or MarketDataService(db)
    if embeddings is None:
        try:
            embeddings = EmbeddingService(db)
        except Exception as exc:
            # Embeddings are an enhancement: without them novelty falls back to
            # the lexical measure and everything else is unaffected.
            log.warning("embedding service unavailable: %s", exc)
    horizon = settings.signal_primary_horizon
    available = market.available_horizons()
    if horizon not in available:
        horizon = available[-1] if available else "1d"

    analysis = event.analysis
    sentiment_model: str | None = None
    if analysis and analysis.status == "COMPLETE" and analysis.sentiment is not None:
        sentiment = float(analysis.sentiment)
        model_confidence = float(analysis.confidence or 0.5)
        sentiment_source = "model"
        sentiment_model = analysis.model
    else:
        # No model reading available: fall back to the rule-based lexicon so the
        # product still works without an API key, and say so in the panel.
        sentiment = relevance.rule_based_sentiment(event.text, event.title)
        # A lexicon reading is inherently less certain than a model reading.
        model_confidence = 0.35
        sentiment_source = "rule_based"

    novelty = historical.novelty(db, event, embeddings=embeddings)

    written: list[Signal] = []
    for link in event.tickers:
        if link.confidence == "LOW" and not include_low_confidence:
            continue
        comparable = historical.build_comparable_set(
            db, market, event=event, ticker=link.ticker, horizons=available,
            embeddings=embeddings,
        )
        stats = comparable.stats.get(horizon)
        result = signals.compute_signal(
            sentiment=sentiment,
            sentiment_source=sentiment_source,
            model_confidence=model_confidence,
            stats=stats,
            novelty_value=novelty.value,
            max_similarity=novelty.max_similarity,
            horizon=horizon,
            ticker_confidence=link.confidence,
            sentiment_model=sentiment_model,
        )

        payload = {
            "horizon": horizon,
            "score": result.score,
            "label": result.label,
            "confidence": result.confidence,
            "sample_size": result.sample_size,
            "sample_flag": result.sample_flag,
            "components": {
                **result.components,
                "contributions": result.contributions,
                "sentiment_source": sentiment_source,
                "sentiment_model": sentiment_model,
                "novelty_raw": novelty.value,
                "max_similarity_30d": novelty.max_similarity,
                "similarity_measure": novelty.measure,
                "most_similar_event_id": novelty.matched_event_id,
                "notes": result.notes,
            },
            "historical_stats": comparable.as_dict(),
            "uncertainties": {"items": result.uncertainties},
            "weights": result.weights,
            "created_at": utcnow(),
        }
        stmt = (
            insert(Signal)
            .values(event_id=event.id, ticker=link.ticker, **payload)
            .on_conflict_do_update(
                constraint="uq_signal_event_ticker",
                set_={k: v for k, v in payload.items() if k != "created_at"},
            )
            .returning(Signal.id)
        )
        signal_id = db.execute(stmt).scalars().first()
        db.flush()
        if signal_id:
            written.append(db.get(Signal, signal_id))
    return written


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------
def process_raw_event(
    db: Session,
    raw: RawEvent,
    *,
    client: AnthropicClient | None = None,
    market: MarketDataService | None = None,
    push_provider: notifications.PushProvider | None = None,
    report: PipelineReport | None = None,
    embeddings: EmbeddingService | None = None,
) -> Event | None:
    report = report or PipelineReport()
    user = primary_user(db)
    values = normalise(raw)
    if not values["text"]:
        raw.processed = True
        raw.process_error = "no text after normalisation"
        return None

    event, created = upsert_event(db, values)
    if event is None:
        raw.processed = True
        raw.process_error = "could not store event"
        return None
    raw.processed = True
    if created:
        report.events_created += 1
    else:
        report.events_duplicate += 1

    # --- RELEVANCE (rules first, then the cheap model) -------------------
    verdict = relevance.score_relevance(event.text, event.title)
    event.relevance_score = max(event.relevance_score or 0.0, verdict.score)
    event.relevance_reason = verdict.reason
    # Archive rows were curated by whoever imported them; they are the sample,
    # so they bypass the relevance gate rather than being silently dropped.
    event.relevant = event.relevant or verdict.relevant or event.is_historical
    if not event.relevant and verdict.score >= settings.triage_relevance_floor:
        # Escalate only genuinely borderline items. Something that matched no
        # market-relevant term at all is not ambiguous, and spending a model
        # call to confirm that is exactly the leak the rule gate exists to plug.
        analysis_mod.triage_event(db, event, client)
    if not event.relevant:
        event.analysis_status = "SKIPPED"
        event.pipeline_stage = "filtered"
        event.processing_timestamp = utcnow()
        report.events_irrelevant += 1
        db.flush()
        return event

    # --- ENTITIES + TICKERS ----------------------------------------------
    aliases = ticker_match.load_aliases(db)
    claude_tickers: list[str] = []

    # --- CLAUDE ANALYSIS (cached by content hash) ------------------------
    if event.is_historical:
        # Never spend tokens re-reading the archive: the event type comes from
        # the import, and these rows are only ever used as historical samples.
        hint = (raw.payload or {}).get("event_type")
        if hint:
            event.event_type = str(hint)
        event.analysis_status = "SKIPPED"
    elif event.analysis_status in ("PENDING", "FAILED"):
        row = analysis_mod.analyse_event(db, event, client)
        report.analyses_run += 1
        if row.parsed:
            claude_tickers = list(row.parsed.get("tickers") or [])
    elif event.analysis and event.analysis.parsed:
        claude_tickers = list(event.analysis.parsed.get("tickers") or [])

    matches = ticker_match.match_tickers(
        event.text, event.title, aliases, claude_tickers=claude_tickers
    )
    # An archive row whose source names a primary ticker keeps it even when the
    # text alone would not resolve one.
    hinted = (raw.payload or {}).get("primary_ticker")
    if hinted and not any(m.ticker == str(hinted).upper() for m in matches):
        matches.append(
            ticker_match.TickerMatch(str(hinted).upper(), "HIGH", "archive hint", "user")
        )
    ticker_match.apply_matches(db, event, matches)
    db.refresh(event)

    # Embed before the signal is built, so novelty can use the vector for this
    # event rather than falling back to the lexical measure on its first pass.
    if embeddings is not None:
        try:
            embeddings.embed_event(event)
        except Exception as exc:
            log.warning("could not embed event %s: %s", event.id, exc)

    if event.is_historical:
        # Archive rows are the *sample*, not the subject: no signal, no alert.
        event.pipeline_stage = "archived"
        event.processing_timestamp = utcnow()
        db.flush()
        return event

    # --- HISTORICAL RESPONSE + SIGNAL ------------------------------------
    written = build_signals_for_event(db, event, market, embeddings=embeddings)
    report.signals_written += len(written)

    # --- NOTIFICATION RULES (only now that the event has a stable ID) ----
    created_notes = notifications.evaluate_event_rules(
        db, user_id=user.id, event=event, signals=written
    )
    report.notifications_created += len(created_notes)
    provider = push_provider or build_push_provider()
    for note in created_notes:
        notifications.deliver_push(db, note, provider)

    event.pipeline_stage = "complete"
    event.processing_timestamp = utcnow()
    db.flush()
    return event


def run_pipeline(
    db: Session,
    *,
    limit: int = 100,
    client: AnthropicClient | None = None,
    market: MarketDataService | None = None,
    push_provider: notifications.PushProvider | None = None,
) -> PipelineReport:
    """Process unprocessed raw events. Safe to call repeatedly."""
    report = PipelineReport()
    job = JobRun(job_name="pipeline")
    db.add(job)
    db.flush()

    market = market or MarketDataService(db)
    client = client or AnthropicClient()
    try:
        embeddings = EmbeddingService(db)
    except Exception as exc:
        log.warning("embeddings disabled for this run: %s", exc)
        embeddings = None

    stmt = (
        select(RawEvent)
        .where(RawEvent.processed.is_(False))
        .order_by(RawEvent.source_timestamp)
        .limit(limit)
    )
    rows = list(db.execute(stmt).scalars())
    report.raw_considered = len(rows)

    for raw in rows:
        try:
            process_raw_event(
                db,
                raw,
                client=client,
                market=market,
                push_provider=push_provider,
                report=report,
                embeddings=embeddings,
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            log.exception("failed to process raw event %s", raw.id)
            report.errors.append(f"{raw.id}: {type(exc).__name__}: {exc}")
            # Mark it processed-with-error so one poison item cannot wedge the
            # queue forever; it stays visible in the data-quality view.
            fresh = db.get(RawEvent, raw.id)
            if fresh:
                fresh.processed = True
                fresh.process_error = f"{type(exc).__name__}: {exc}"[:2000]
                db.commit()

    job.status = "FAILED" if report.errors else "SUCCESS"
    job.finished_at = utcnow()
    job.items_processed = report.raw_considered
    job.detail = report.as_dict()
    job.error = "; ".join(report.errors[:5]) or None
    db.commit()
    return report


def retry_failed_analyses(
    db: Session, *, client: AnthropicClient | None = None, market: MarketDataService | None = None
) -> int:
    """Retry analyses that failed earlier, capped by `analysis_attempts`."""
    client = client or AnthropicClient()
    if not client.available:
        return 0
    market = market or MarketDataService(db)
    count = 0
    for event in analysis_mod.retryable_events(db):
        row = analysis_mod.analyse_event(db, event, client)
        if row.status == "COMPLETE":
            build_signals_for_event(db, event, market)
            count += 1
        db.commit()
    return count


def check_source_health(db: Session) -> list[Notification]:
    """Raise (and clear) system alerts for sources that are failing or recovered."""
    user = primary_user(db)
    created: list[Notification] = []
    for health in db.execute(select(SourceHealth)).scalars():
        if health.status == "ERROR":
            note = notifications.system_alert(
                db,
                user_id=user.id,
                condition=f"source_down:{health.source_key}:{health.consecutive_failures}",
                title="System alert",
                body=(
                    f"Source '{health.source_key}' has failed "
                    f"{health.consecutive_failures} times in a row. "
                    f"Last error: {(health.last_error or 'unknown')[:200]}"
                ),
                severity="critical",
                window=notifications.hour_window(),
            )
            if note:
                created.append(note)
        elif health.status == "ONLINE" and health.last_success_at:
            stale = utcnow() - health.last_success_at > dt.timedelta(hours=6)
            if stale:
                note = notifications.system_alert(
                    db,
                    user_id=user.id,
                    condition=f"source_stale:{health.source_key}",
                    title="System alert",
                    body=f"Source '{health.source_key}' has not returned data for over 6 hours.",
                    severity="warning",
                    window=notifications.day_window(),
                )
                if note:
                    created.append(note)
    db.commit()
    return created
