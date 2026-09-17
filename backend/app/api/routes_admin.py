"""Operational endpoints: config, source health, manual runs, import, data quality."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from ..auth import current_user
from ..config import settings
from ..db import get_db
from ..embeddings.service import EmbeddingService, build_provider as build_embedding_provider
from ..llm.fake import build_client
from ..market.service import MarketDataService, build_provider
from ..models import (
    ClaudeAnalysis,
    Event,
    EventEmbedding,
    EventTicker,
    JobRun,
    LLMUsage,
    NotificationDelivery,
    PushSubscription,
    RawEvent,
    Source,
    SourceHealth,
    User,
    utcnow,
)
from ..pipeline import runner
from ..seed import loader
from ..sources import registry

router = APIRouter(prefix="/api", tags=["admin"])


@router.get("/config")
def public_config() -> dict:
    """Public, unauthenticated configuration.

    Contains nothing secret: the Web Push *public* key is public by design, and
    everything else here is a capability flag the UI needs before login.
    """
    return {
        "app_name": settings.app_name,
        "environment": settings.environment,
        "web_push_public_key": settings.web_push_public_key,
        "push_configured": settings.push_enabled,
        "email_configured": settings.email_enabled,
        "llm_configured": settings.llm_enabled or settings.llm_fake_mode,
        "llm_mode": "canned-mock" if settings.llm_fake_mode else ("live" if settings.llm_enabled else "disabled"),
        "market_provider": settings.market_data_provider,
        "supports_intraday": build_provider().supports_intraday(),
        "embedding_provider": settings.embedding_provider,
        "embedding_semantic": build_embedding_provider().semantic,
        "embedding_label": build_embedding_provider().label,
        "signal_weights": settings.signal_weights(),
        "signal_thresholds": settings.signal_thresholds(),
        "signal_return_scale": settings.signal_return_scale,
        "sample_size_gates": {
            "unreliable_below": settings.min_usable_sample,
            "limited_below": settings.min_reliable_sample,
        },
        "primary_horizon": settings.signal_primary_horizon,
        "phase": 2,
    }


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    """Liveness + a worker heartbeat. Unauthenticated so a monitor can poll it."""
    try:
        db.execute(select(func.count(Event.id)))
        database_ok = True
    except Exception:
        database_ok = False

    last_job = (
        db.execute(select(JobRun).order_by(desc(JobRun.started_at)).limit(1)).scalars().first()
    )
    worker_stale = True
    if last_job:
        worker_stale = utcnow() - last_job.started_at > dt.timedelta(minutes=30)
    return {
        "status": "ok" if database_ok else "degraded",
        "database": database_ok,
        "last_job": (
            {
                "name": last_job.job_name,
                "status": last_job.status,
                "started_at": last_job.started_at.isoformat(),
                "items": last_job.items_processed,
            }
            if last_job
            else None
        ),
        "worker_stale": worker_stale,
    }


@router.get("/sources")
def sources(db: Session = Depends(get_db), _: User = Depends(current_user)) -> dict:
    rows = list(db.execute(select(Source).order_by(Source.key)).scalars())
    health_rows = {h.source_key: h for h in db.execute(select(SourceHealth)).scalars()}
    return {
        "items": [
            {
                "key": s.key,
                "name": s.name,
                "kind": s.kind,
                "enabled": s.enabled,
                "priority": s.priority,
                "poll_interval_minutes": s.poll_interval_minutes,
                "status": health_rows[s.key].status if s.key in health_rows else "UNKNOWN",
                "consecutive_failures": (
                    health_rows[s.key].consecutive_failures if s.key in health_rows else 0
                ),
                "last_success_at": (
                    health_rows[s.key].last_success_at.isoformat()
                    if s.key in health_rows and health_rows[s.key].last_success_at
                    else None
                ),
                "last_error": health_rows[s.key].last_error if s.key in health_rows else None,
                "items_last_poll": (
                    health_rows[s.key].items_last_poll if s.key in health_rows else 0
                ),
            }
            for s in rows
        ]
    }


@router.post("/admin/poll")
def trigger_poll(db: Session = Depends(get_db), _: User = Depends(current_user)) -> dict:
    results = registry.poll_all(db)
    db.commit()
    return {"results": results}


@router.post("/admin/pipeline")
def trigger_pipeline(
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
    limit: int = Query(200, ge=1, le=2000),
) -> dict:
    report = runner.run_pipeline(db, limit=limit, client=build_client())
    return report.as_dict()


@router.post("/admin/seed")
def trigger_seed(
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
    include_archive: bool = True,
) -> dict:
    counts = loader.seed_reference_data(db)
    loader.seed_user(db)
    imported = loader.seed_sample_archive(db) if include_archive else 0
    return {"reference": counts, "archive_rows_imported": imported}


@router.post("/admin/import")
def import_archive(
    body: dict, db: Session = Depends(get_db), _: User = Depends(current_user)
) -> dict:
    """Import an archive of past posts/announcements.

    Body: ``{"source": "truth_social", "rows": [...]}``. Re-importing the same
    rows inserts nothing.
    """
    rows = (body or {}).get("rows")
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=422, detail="rows must be a non-empty list")
    if len(rows) > 20000:
        raise HTTPException(status_code=413, detail="import at most 20000 rows per request")
    source_key = str((body or {}).get("source") or "archive")[:64]
    inserted = loader.import_archive(db, rows, source_key=source_key)
    return {"received": len(rows), "inserted": inserted}


@router.post("/admin/embed")
def trigger_embedding_backfill(
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
    limit: int = Query(500, ge=1, le=5000),
) -> dict:
    """Embed events that have no vector, or whose vector is stale."""
    service = EmbeddingService(db)
    written = service.backfill(limit=limit)
    db.commit()
    remaining = len(service.pending_events(limit=5000))
    return {
        "embedded": written,
        "remaining": remaining,
        "provider": service.provider.name,
        "model": service.model_name,
        "dim": service.provider.dim,
    }


@router.post("/admin/push-retry")
def trigger_push_retry(db: Session = Depends(get_db), _: User = Depends(current_user)) -> dict:
    """Retry due push deliveries and clean up long-dead subscriptions."""
    from ..pipeline.push import cleanup_expired_subscriptions, retry_failed_deliveries

    result = retry_failed_deliveries(db)
    result["subscriptions_pruned"] = cleanup_expired_subscriptions(db)
    return result


@router.get("/admin/embeddings")
def embedding_status(db: Session = Depends(get_db), _: User = Depends(current_user)) -> dict:
    service = EmbeddingService(db)
    total_events = int(db.execute(select(func.count(Event.id))).scalar() or 0)
    embedded = int(
        db.execute(
            select(func.count(EventEmbedding.event_id)).where(
                EventEmbedding.provider == service.provider.name
            )
        ).scalar()
        or 0
    )
    by_provider = dict(
        db.execute(
            select(EventEmbedding.provider, func.count(EventEmbedding.event_id)).group_by(
                EventEmbedding.provider
            )
        ).all()
    )
    return {
        "provider": service.provider.name,
        "model": service.model_name,
        "dim": service.provider.dim,
        "semantic": service.provider.semantic,
        "measure": service.provider.label,
        "similarity_threshold": service.threshold,
        "events_total": total_events,
        "events_embedded": embedded,
        "events_pending": max(total_events - embedded, 0),
        "rows_by_provider": by_provider,
    }


@router.get("/admin/data-quality")
def data_quality(db: Session = Depends(get_db), _: User = Depends(current_user)) -> dict:
    """Everything that is wrong or missing, in one place."""
    since = utcnow() - dt.timedelta(days=7)

    unprocessed = int(
        db.execute(
            select(func.count(RawEvent.id)).where(RawEvent.processed.is_(False))
        ).scalar()
        or 0
    )
    process_errors = list(
        db.execute(
            select(RawEvent)
            .where(RawEvent.process_error.is_not(None))
            .order_by(desc(RawEvent.ingestion_timestamp))
            .limit(20)
        ).scalars()
    )
    malformed = list(
        db.execute(
            select(ClaudeAnalysis)
            .where(ClaudeAnalysis.status == "FAILED")
            .order_by(desc(ClaudeAnalysis.created_at))
            .limit(20)
        ).scalars()
    )
    pending_analyses = int(
        db.execute(
            select(func.count(Event.id)).where(
                Event.relevant.is_(True), Event.analysis_status.in_(["PENDING", "FAILED"])
            )
        ).scalar()
        or 0
    )
    # Duplicate detection: raw rows whose content hash already produced an event
    # under a different external id.
    duplicate_hashes = int(
        db.execute(
            select(func.count())
            .select_from(
                select(RawEvent.content_hash)
                .group_by(RawEvent.content_hash)
                .having(func.count(RawEvent.id) > 1)
                .subquery()
            )
        ).scalar()
        or 0
    )
    # Timestamp sanity: anything from the future, or ingested before it happened.
    future_events = int(
        db.execute(
            select(func.count(Event.id)).where(
                Event.source_timestamp > utcnow() + dt.timedelta(hours=2)
            )
        ).scalar()
        or 0
    )
    corrections = int(
        db.execute(
            select(func.count(EventTicker.id)).where(EventTicker.user_corrected.is_(True))
        ).scalar()
        or 0
    )
    low_confidence = int(
        db.execute(
            select(func.count(EventTicker.id)).where(EventTicker.confidence == "LOW")
        ).scalar()
        or 0
    )
    failed_deliveries = list(
        db.execute(
            select(NotificationDelivery)
            .where(NotificationDelivery.status.in_(["FAILED", "EXPIRED"]))
            .order_by(desc(NotificationDelivery.created_at))
            .limit(20)
        ).scalars()
    )
    expired_subs = int(
        db.execute(
            select(func.count(PushSubscription.id)).where(PushSubscription.active.is_(False))
        ).scalar()
        or 0
    )
    failed_jobs = list(
        db.execute(
            select(JobRun)
            .where(JobRun.status == "FAILED", JobRun.started_at >= since)
            .order_by(desc(JobRun.started_at))
            .limit(10)
        ).scalars()
    )
    stale_sources = [
        h.source_key
        for h in db.execute(select(SourceHealth)).scalars()
        if h.last_success_at is None
        or utcnow() - h.last_success_at > dt.timedelta(hours=6)
    ]

    # Stale market data: the newest daily bar we hold, per context symbol.
    market = MarketDataService(db)
    stale_market = []
    for symbol in settings.market_context_symbols:
        latest = market.close_on_or_before(symbol, utcnow())
        stale_market.append(
            {
                "symbol": symbol,
                "latest_bar": latest[0].isoformat() if latest else None,
                "stale": latest is None or (utcnow() - latest[0]).days > 5,
            }
        )

    usage = db.execute(
        select(
            func.count(LLMUsage.id),
            func.coalesce(func.sum(LLMUsage.input_tokens), 0),
            func.coalesce(func.sum(LLMUsage.output_tokens), 0),
            func.coalesce(func.sum(LLMUsage.estimated_cost_usd), 0.0),
        ).where(LLMUsage.created_at >= since)
    ).one()

    # Per-source volume and the model spend attributable to it. A high-volume
    # source that is mostly noise -- Congress is the one built to be -- shows up
    # here as a lot of events for very little spend if its gate is working, and
    # as a lot of spend if it is not. `llm_usage` has no source column, so the
    # attribution is a join through the event; calls not tied to an event (there
    # are none today) would simply not appear.
    by_source = db.execute(
        select(
            Event.source_key,
            func.count(func.distinct(Event.id)).label("events"),
            func.count(func.distinct(Event.id)).filter(Event.relevant.is_(True)).label("relevant"),
        )
        .where(Event.ingestion_timestamp >= since)
        .group_by(Event.source_key)
    ).all()

    spend_by_source = dict(
        db.execute(
            select(
                Event.source_key,
                func.coalesce(func.sum(LLMUsage.estimated_cost_usd), 0.0),
            )
            .join(Event, Event.id == LLMUsage.event_id)
            .where(LLMUsage.created_at >= since)
            .group_by(Event.source_key)
        ).all()
    )
    calls_by_source = dict(
        db.execute(
            select(Event.source_key, func.count(LLMUsage.id))
            .join(Event, Event.id == LLMUsage.event_id)
            .where(LLMUsage.created_at >= since)
            .group_by(Event.source_key)
        ).all()
    )

    embedded = int(
        db.execute(
            select(func.count(EventEmbedding.event_id)).where(
                EventEmbedding.provider == settings.embedding_provider
            )
        ).scalar()
        or 0
    )
    total_events = int(db.execute(select(func.count(Event.id))).scalar() or 0)

    return {
        "unprocessed_raw_events": unprocessed,
        "events_without_current_embedding": max(total_events - embedded, 0),
        "duplicate_content_hashes": duplicate_hashes,
        "future_timestamps": future_events,
        "pending_or_failed_analyses": pending_analyses,
        "ticker_corrections": corrections,
        "low_confidence_ticker_matches": low_confidence,
        "expired_push_subscriptions": expired_subs,
        "stale_sources": stale_sources,
        "market_data": stale_market,
        "process_errors": [
            {"id": r.id, "source": r.source_key, "error": r.process_error}
            for r in process_errors
        ],
        "malformed_analyses": [
            {
                "id": a.id,
                "event_id": a.event_id,
                "model": a.model,
                "attempts": a.attempts,
                "error": a.validation_error,
            }
            for a in malformed
        ],
        "failed_deliveries": [
            {
                "channel": d.channel,
                "status": d.status,
                "response": d.provider_response,
                "created_at": d.created_at.isoformat(),
            }
            for d in failed_deliveries
        ],
        "failed_jobs": [
            {"name": j.job_name, "started_at": j.started_at.isoformat(), "error": j.error}
            for j in failed_jobs
        ],
        "llm_usage_7d": {
            "calls": usage[0],
            "input_tokens": int(usage[1]),
            "output_tokens": int(usage[2]),
            "estimated_cost_usd": round(float(usage[3]), 4),
            "daily_call_budget": settings.llm_daily_call_budget,
        },
        "by_source_7d": sorted(
            (
                {
                    "source": row.source_key,
                    "events": int(row.events),
                    "relevant": int(row.relevant or 0),
                    "model_calls": int(calls_by_source.get(row.source_key, 0)),
                    "estimated_cost_usd": round(
                        float(spend_by_source.get(row.source_key, 0.0)), 4
                    ),
                }
                for row in by_source
            ),
            key=lambda r: (-r["events"], r["source"]),
        ),
    }


@router.get("/admin/jobs")
def jobs(
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
    limit: int = Query(25, ge=1, le=100),
) -> dict:
    rows = list(
        db.execute(select(JobRun).order_by(desc(JobRun.started_at)).limit(limit)).scalars()
    )
    return {
        "items": [
            {
                "id": j.id,
                "job_name": j.job_name,
                "status": j.status,
                "started_at": j.started_at.isoformat(),
                "finished_at": j.finished_at.isoformat() if j.finished_at else None,
                "items_processed": j.items_processed,
                "detail": j.detail,
                "error": j.error,
            }
            for j in rows
        ]
    }
