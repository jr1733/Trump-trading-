"""Deduplication: at ingestion, across sources, and across pipeline re-runs."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from app.models import Event, RawEvent
from app.pipeline.runner import normalise, upsert_event
from app.sources.base import RawItem, content_hash
from app.sources.registry import store_raw_items

UTC = dt.timezone.utc


def item(external_id: str, text: str, *, source: str = "mock", title: str | None = None) -> RawItem:
    return RawItem(
        source_key=source,
        external_id=external_id,
        title=title,
        text=text,
        source_timestamp=dt.datetime(2026, 3, 4, 15, 0, tzinfo=UTC),
        payload={"text": text, "title": title},
    )


def test_content_hash_ignores_whitespace_and_case():
    assert content_hash("Title", "Some   text\nhere") == content_hash("title", "Some text here")


def test_content_hash_distinguishes_different_text():
    assert content_hash(None, "tariffs on chips") != content_hash(None, "tariffs on cars")


def test_store_raw_items_is_idempotent_on_source_and_external_id(db):
    items = [item("a1", "tariffs on chips"), item("a2", "sanctions on oil")]
    assert store_raw_items(db, items) == 2
    db.commit()

    # Re-polling the same feed returns the same items: nothing new is inserted.
    assert store_raw_items(db, items) == 0
    db.commit()
    assert db.execute(select(func.count(RawEvent.id))).scalar() == 2


def test_same_content_from_two_sources_becomes_one_event(db):
    text = "Tariffs on imported semiconductors are under review."
    store_raw_items(db, [item("wh-1", text, source="whitehouse")])
    store_raw_items(db, [item("news-1", text, source="news_rss")])
    db.commit()

    raws = list(db.execute(select(RawEvent).order_by(RawEvent.source_key)).scalars())
    assert len(raws) == 2

    first, created_first = upsert_event(db, normalise(raws[0]))
    second, created_second = upsert_event(db, normalise(raws[1]))
    db.commit()

    assert created_first is True
    assert created_second is False, "identical content must not create a second event"
    assert first.id == second.id
    assert db.execute(select(func.count(Event.id))).scalar() == 1


def test_upsert_event_returns_existing_row_on_conflict(db):
    store_raw_items(db, [item("x1", "Sanctions announced on crude exports.")])
    db.commit()
    raw = db.execute(select(RawEvent)).scalars().one()

    event, created = upsert_event(db, normalise(raw))
    db.commit()
    assert created is True

    again, created_again = upsert_event(db, normalise(raw))
    db.commit()
    assert created_again is False
    assert again.id == event.id
