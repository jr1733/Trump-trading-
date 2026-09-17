"""purge-mock, and the guarantee that `seed` is safe to run on production.

`docker compose` runs `manage.py seed` on every start, including production. If
seed ever starts loading events or prices, a production database quietly gains
invented history and every statistic computed from it is wrong -- so that is
asserted here rather than left as a convention.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from app.models import (
    AlertRule,
    ClaudeAnalysis,
    Entity,
    EntityAlias,
    Event,
    EventTicker,
    HistoricalEventMatch,
    MarketPrice,
    Notification,
    RawEvent,
    Signal,
    Ticker,
    User,
    Watchlist,
)
from app.seed import loader, purge

UTC = dt.timezone.utc


def count(db, model) -> int:
    return int(db.execute(select(func.count()).select_from(model)).scalar() or 0)


def build_mixed_database(db):
    """One synthetic event and one real one, each with everything hanging off."""
    owner = db.execute(select(User)).scalars().first()
    if owner is None:
        owner = User(display_name="Operator")
        db.add(owner)
        db.flush()
    made = {}
    for key, external in (("mock", "m1"), ("whitehouse", "w1")):
        event = Event(
            source_key=key,
            external_id=external,
            title=f"{key} title",
            text=f"{key} tariffs on chips",
            content_hash=f"hash-{key}",
            source_timestamp=dt.datetime(2026, 3, 4, 15, 0, tzinfo=UTC),
            relevant=True,
        )
        db.add(event)
        db.flush()
        db.add(RawEvent(
            source_key=key, external_id=external, payload={}, content_hash=f"hash-{key}",
            source_timestamp=event.source_timestamp,
        ))
        db.add(EventTicker(event_id=event.id, ticker="NVDA", confidence="HIGH"))
        db.add(ClaudeAnalysis(
            event_id=event.id, content_hash=event.content_hash, model="canned-mock", mode="fake",
        ))
        db.add(Signal(
            event_id=event.id, ticker="NVDA", horizon="1d", score=0.5, label="BULLISH",
            sample_size=12, sample_flag="limited",
        ))
        db.add(Notification(
            user_id=owner.id, event_id=event.id, notification_type="new_event",
            title="t", body="b", idempotency_key=f"idem-{key}",
        ))
        made[key] = event
    # A comparable-event match between the two.
    db.add(HistoricalEventMatch(
        event_id=made["whitehouse"].id, matched_event_id=made["mock"].id,
        ticker="NVDA", similarity=0.8,
    ))
    # Distinct days: market_prices is UNIQUE(symbol, interval, ts), so the two
    # providers cannot both own the same bar.
    for day, provider in ((4, "mock"), (5, "stooq")):
        db.add(MarketPrice(
            symbol="SPY", interval="1d", ts=dt.datetime(2026, 3, day, 14, 30, tzinfo=UTC),
            open=1, high=1, low=1, close=1, adjusted_close=1, volume=1, provider=provider,
        ))
    db.commit()
    return made


# --- counting -------------------------------------------------------------
def test_dry_run_counts_only_the_synthetic_rows(db):
    build_mixed_database(db)
    counts = purge.count_synthetic(db)

    assert counts["events"] == 1
    assert counts["raw_events"] == 1
    assert counts["signals"] == 1
    assert counts["analyses"] == 1
    assert counts["event_tickers"] == 1
    assert counts["notifications"] == 1
    assert counts["market_prices"] == 1
    assert counts["historical_matches"] == 1, "a match touching a mock event counts"


def test_counting_deletes_nothing(db):
    build_mixed_database(db)
    before = count(db, Event)
    purge.count_synthetic(db)
    assert count(db, Event) == before


# --- deleting -------------------------------------------------------------
def test_purge_removes_synthetic_and_keeps_real(db):
    build_mixed_database(db)
    purge.purge_synthetic(db)

    remaining = list(db.execute(select(Event)).scalars())
    assert [e.source_key for e in remaining] == ["whitehouse"]

    assert count(db, Signal) == 1
    assert count(db, ClaudeAnalysis) == 1
    assert count(db, RawEvent) == 1
    assert count(db, EventTicker) == 1

    providers = set(db.execute(select(MarketPrice.provider)).scalars())
    assert providers == {"stooq"}, "mock bars go, real ones stay"


def test_a_match_pointing_at_a_purged_event_goes_too(db):
    """The match belongs to the real event but references the mock one. Leaving
    it would mean a real event's comparable set silently contains a ghost."""
    build_mixed_database(db)
    purge.purge_synthetic(db)
    assert count(db, HistoricalEventMatch) == 0


def test_notifications_about_purged_events_are_deleted_not_orphaned(db):
    """notifications.event_id is ON DELETE SET NULL, so without an explicit
    delete these survive pointing at nothing."""
    build_mixed_database(db)
    purge.purge_synthetic(db)

    rows = list(db.execute(select(Notification)).scalars())
    assert len(rows) == 1
    assert rows[0].event_id is not None


def test_all_notifications_clears_eventless_ones_too(db):
    build_mixed_database(db)
    owner = db.execute(select(User)).scalars().first()
    db.add(Notification(
        user_id=owner.id, event_id=None, notification_type="digest",
        title="Daily digest", body="b", idempotency_key="idem-digest",
    ))
    db.commit()

    purge.purge_synthetic(db, all_notifications=True)
    assert count(db, Notification) == 0


def test_purge_is_idempotent(db):
    build_mixed_database(db)
    purge.purge_synthetic(db)
    second = purge.purge_synthetic(db)
    assert sum(second.values()) == 0


def test_purging_an_empty_database_is_a_no_op(db):
    assert sum(purge.purge_synthetic(db).values()) == 0


# --- seed is production-safe ----------------------------------------------
def test_seed_loads_no_events_and_no_prices(db):
    """docker-compose runs this on every start, production included."""
    loader.seed_reference_data(db)
    loader.seed_user(db)

    assert count(db, Event) == 0, "seed must never create an event"
    assert count(db, RawEvent) == 0
    assert count(db, MarketPrice) == 0, "seed must never create a price bar"
    assert count(db, Signal) == 0


def test_seed_loads_the_reference_data_the_app_needs(db):
    loader.seed_reference_data(db)
    user = loader.seed_user(db)

    assert count(db, Ticker) > 0
    assert count(db, EntityAlias) > 0
    assert count(db, Entity) > 0
    assert count(db, User) == 1 and user is not None
    assert count(db, Watchlist) == 1
    assert count(db, AlertRule) > 0


def test_seed_is_idempotent(db):
    loader.seed_reference_data(db)
    loader.seed_user(db)
    tickers, users = count(db, Ticker), count(db, User)

    loader.seed_reference_data(db)
    loader.seed_user(db)

    assert count(db, Ticker) == tickers
    assert count(db, User) == users, "a second start must not create a second user"


def test_purge_leaves_a_seeded_database_ready_to_use(db):
    """The production sequence: seed, then purge whatever dev data arrived."""
    loader.seed_reference_data(db)
    loader.seed_user(db)
    build_mixed_database(db)  # reuses the seeded user

    purge.purge_synthetic(db)

    assert count(db, Ticker) > 0
    assert count(db, User) == 1
    assert count(db, AlertRule) > 0
    assert count(db, Watchlist) == 1
