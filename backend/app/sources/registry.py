"""Source registry and the poller.

The poller's contract: **one source failing never affects another.** Every
adapter runs inside its own try/except, writes its own `source_health` row, and
raises nothing to the caller.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..config import settings
from ..models import RawEvent, Source, SourceHealth, utcnow
from .base import RawItem, SourceAdapter
from .federal_register import FederalRegisterAdapter
from .mock_source import MockSourceAdapter
from .news_rss import NewsRSSAdapter
from .truth_social import TruthSocialAdapter
from .whitehouse import WhiteHouseAdapter

log = logging.getLogger(__name__)

ADAPTERS: dict[str, type[SourceAdapter]] = {
    MockSourceAdapter.key: MockSourceAdapter,
    WhiteHouseAdapter.key: WhiteHouseAdapter,
    FederalRegisterAdapter.key: FederalRegisterAdapter,
    NewsRSSAdapter.key: NewsRSSAdapter,
    TruthSocialAdapter.key: TruthSocialAdapter,
}

PRIORITY_INTERVALS = {
    "high": lambda: settings.poll_interval_high_minutes,
    "normal": lambda: settings.poll_interval_normal_minutes,
    "slow": lambda: settings.poll_interval_slow_minutes,
}


def build_adapters(keys: list[str] | None = None) -> list[SourceAdapter]:
    wanted = keys if keys is not None else settings.enabled_sources
    adapters: list[SourceAdapter] = []
    for key in wanted:
        cls = ADAPTERS.get(key)
        if cls is None:
            log.warning("unknown source %r in ENABLED_SOURCES; ignoring", key)
            continue
        adapters.append(cls())
    return adapters


def sync_source_rows(db: Session) -> None:
    """Make sure every known adapter has a `sources` row."""
    for key, cls in ADAPTERS.items():
        existing = db.execute(select(Source).where(Source.key == key)).scalars().first()
        interval = PRIORITY_INTERVALS.get(cls.priority, PRIORITY_INTERVALS["normal"])()
        if existing is None:
            db.add(
                Source(
                    key=key,
                    name=cls.name,
                    kind=cls.kind,
                    priority=cls.priority,
                    poll_interval_minutes=interval,
                    enabled=key in settings.enabled_sources,
                )
            )
    db.flush()


def get_health(db: Session, source_key: str) -> SourceHealth:
    row = (
        db.execute(select(SourceHealth).where(SourceHealth.source_key == source_key))
        .scalars()
        .first()
    )
    if row is None:
        row = SourceHealth(source_key=source_key)
        db.add(row)
        db.flush()
    return row


def record_success(db: Session, source_key: str, item_count: int) -> SourceHealth:
    health = get_health(db, source_key)
    health.status = "ONLINE"
    health.last_success_at = utcnow()
    health.last_attempt_at = health.last_success_at
    health.consecutive_failures = 0
    health.last_error = None
    health.items_last_poll = item_count
    health.updated_at = utcnow()
    db.flush()
    return health


def record_failure(db: Session, source_key: str, error: str) -> SourceHealth:
    health = get_health(db, source_key)
    health.consecutive_failures += 1
    health.last_attempt_at = utcnow()
    health.last_error = error[:2000]
    # One blip is DEGRADED; sustained failure is ERROR and raises a system alert.
    health.status = (
        "ERROR"
        if health.consecutive_failures >= settings.source_failure_alert_threshold
        else "DEGRADED"
    )
    health.updated_at = utcnow()
    db.flush()
    return health


def store_raw_items(db: Session, items: list[RawItem]) -> int:
    """Insert raw items, skipping ones already seen. Returns the new-row count.

    Deduplication here is on ``(source_key, external_id)`` -- the same item
    re-appearing in a feed must not create a second row. Cross-source dedup on
    content happens later, in the pipeline.
    """
    if not items:
        return 0
    rows = [
        {
            "source_key": item.source_key,
            "external_id": item.external_id[:512],
            "payload": item.payload,
            "content_hash": item.content_hash,
            "source_timestamp": item.source_timestamp,
            "ingestion_timestamp": utcnow(),
            "processed": False,
        }
        for item in items
    ]
    stmt = (
        insert(RawEvent)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_raw_events_source_external")
        .returning(RawEvent.id)
    )
    inserted = list(db.execute(stmt).scalars())
    db.flush()
    return len(inserted)


def poll_source(db: Session, adapter: SourceAdapter) -> dict:
    """Poll one adapter. Never raises."""
    result = {"source": adapter.key, "new_items": 0, "fetched": 0, "status": "ONLINE"}
    if not adapter.enabled():
        health = get_health(db, adapter.key)
        health.status = "MANUAL_ONLY" if adapter.kind == "manual" else "DISABLED"
        health.last_attempt_at = utcnow()
        health.updated_at = utcnow()
        db.flush()
        result["status"] = health.status
        return result
    try:
        items = adapter.fetch()
    except Exception as exc:
        health = record_failure(db, adapter.key, f"{type(exc).__name__}: {exc}")
        log.warning("source %s failed: %s", adapter.key, exc)
        result["status"] = health.status
        result["error"] = str(exc)
        return result

    result["fetched"] = len(items)
    result["new_items"] = store_raw_items(db, items)
    record_success(db, adapter.key, len(items))
    return result


def poll_all(db: Session, adapters: list[SourceAdapter] | None = None) -> list[dict]:
    sync_source_rows(db)
    return [poll_source(db, adapter) for adapter in (adapters or build_adapters())]


def due_adapters(db: Session, now: dt.datetime | None = None) -> list[SourceAdapter]:
    """Adapters whose poll interval has elapsed since their last attempt."""
    moment = now or utcnow()
    due: list[SourceAdapter] = []
    for adapter in build_adapters():
        interval = PRIORITY_INTERVALS.get(adapter.priority, PRIORITY_INTERVALS["normal"])()
        health = (
            db.execute(select(SourceHealth).where(SourceHealth.source_key == adapter.key))
            .scalars()
            .first()
        )
        if health is None or health.last_attempt_at is None:
            due.append(adapter)
            continue
        if moment - health.last_attempt_at >= dt.timedelta(minutes=interval):
            due.append(adapter)
    return due
