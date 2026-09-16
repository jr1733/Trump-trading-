"""Look-ahead prevention.

The property under test: a historical statistic for an event at time T can only
ever be built from events strictly before T. Getting this wrong makes every
backtest meaningless, so it is enforced in `find_comparable_events` rather than
being left to callers to remember.
"""

from __future__ import annotations

import datetime as dt

from app.market.service import MarketDataService
from app.models import Event, EventTicker, Ticker
from app.pipeline.historical import build_comparable_set, find_comparable_events, novelty

UTC = dt.timezone.utc


def add_event(db, *, when: dt.datetime, ticker="AAPL", event_type="tariff", confidence="HIGH",
              text="Tariffs on consumer electronics are under review.") -> Event:
    event = Event(
        source_key="archive",
        external_id=f"e-{when.isoformat()}-{ticker}",
        title="Tariff statement",
        text=f"{text} ({when.date()})",
        content_hash=f"h-{when.isoformat()}-{ticker}-{event_type}-{text[:24]}",
        event_type=event_type,
        source_timestamp=when,
        relevant=True,
        is_historical=True,
    )
    db.add(event)
    db.flush()
    db.add(EventTicker(event_id=event.id, ticker=ticker, confidence=confidence))
    db.commit()
    db.refresh(event)
    return event


def setup_timeline(db) -> list[Event]:
    db.add(Ticker(symbol="AAPL", name="Apple Inc.", sector="Technology", sector_etf="XLK"))
    db.add(Ticker(symbol="MSFT", name="Microsoft", sector="Technology", sector_etf="XLK"))
    db.add(Ticker(symbol="XLK", name="Tech sector", asset_class="etf"))
    db.add(Ticker(symbol="SPY", name="S&P 500", asset_class="etf"))
    db.commit()
    return [
        add_event(db, when=dt.datetime(2026, m, 4, 15, 0, tzinfo=UTC)) for m in range(1, 9)
    ]


def test_only_earlier_events_are_comparable(db):
    events = setup_timeline(db)
    subject = events[4]  # May

    past, _ = find_comparable_events(
        db, event_type="tariff", ticker="AAPL", before=subject.source_timestamp,
        exclude_event_id=subject.id,
    )
    assert past, "expected some history"
    assert all(e.source_timestamp < subject.source_timestamp for e in past)
    assert len(past) == 4  # Jan..Apr


def test_the_subject_event_is_never_in_its_own_sample(db):
    events = setup_timeline(db)
    subject = events[3]
    past, _ = find_comparable_events(
        db, event_type="tariff", ticker="AAPL", before=subject.source_timestamp,
        exclude_event_id=subject.id,
    )
    assert subject.id not in {e.id for e in past}


def test_an_event_at_the_exact_same_timestamp_is_excluded(db):
    setup_timeline(db)
    twin_time = dt.datetime(2026, 5, 4, 15, 0, tzinfo=UTC)
    twin = add_event(db, when=twin_time, ticker="AAPL", text="A second statement the same moment")

    past, _ = find_comparable_events(
        db, event_type="tariff", ticker="AAPL", before=twin_time, exclude_event_id=twin.id
    )
    assert all(e.source_timestamp < twin_time for e in past)


def test_the_earliest_event_has_an_empty_sample(db):
    events = setup_timeline(db)
    first = events[0]
    past, _ = find_comparable_events(
        db, event_type="tariff", ticker="AAPL", before=first.source_timestamp,
        exclude_event_id=first.id,
    )
    assert past == []


def test_comparable_set_statistics_respect_the_cutoff(db):
    events = setup_timeline(db)
    subject = events[2]  # March
    market = MarketDataService(db)
    result = build_comparable_set(db, market, event=subject, ticker="AAPL", horizons=["1d"])
    db.commit()

    assert result.stats["1d"].n <= 2
    for match in result.matches:
        assert match["source_timestamp"] < subject.source_timestamp.isoformat()


def test_different_event_type_is_not_comparable(db):
    setup_timeline(db)
    subject = add_event(
        db, when=dt.datetime(2026, 9, 4, 15, 0, tzinfo=UTC), event_type="sanction",
        text="Sanctions announced",
    )
    past, basis = find_comparable_events(
        db, event_type="sanction", ticker="AAPL", before=subject.source_timestamp,
        exclude_event_id=subject.id,
    )
    assert past == []


def test_sector_fallback_is_used_only_when_the_ticker_has_no_history(db):
    setup_timeline(db)
    # MSFT has no tariff history of its own, but shares XLK with AAPL.
    subject = add_event(db, when=dt.datetime(2026, 9, 4, 15, 0, tzinfo=UTC), ticker="MSFT")
    past, basis = find_comparable_events(
        db, event_type="tariff", ticker="MSFT", before=subject.source_timestamp,
        exclude_event_id=subject.id,
    )
    assert basis == "event_type+sector"
    assert past
    assert all(e.source_timestamp < subject.source_timestamp for e in past)


def test_low_confidence_ticker_links_are_excluded_from_statistics(db):
    setup_timeline(db)
    add_event(
        db, when=dt.datetime(2026, 2, 20, 15, 0, tzinfo=UTC), confidence="LOW",
        text="An apple a day",
    )
    subject = add_event(db, when=dt.datetime(2026, 9, 4, 15, 0, tzinfo=UTC))
    past, _ = find_comparable_events(
        db, event_type="tariff", ticker="AAPL", before=subject.source_timestamp,
        exclude_event_id=subject.id,
    )
    assert all("An apple a day" not in e.text for e in past)


def test_novelty_only_looks_backwards(db):
    setup_timeline(db)
    subject = add_event(
        db, when=dt.datetime(2026, 8, 20, 15, 0, tzinfo=UTC),
        text="Tariffs on consumer electronics are under review.",
    )
    # A near-identical event *after* the subject must not reduce its novelty.
    add_event(
        db, when=dt.datetime(2026, 9, 1, 15, 0, tzinfo=UTC),
        text="Tariffs on consumer electronics are under review.",
    )
    value, max_similarity = novelty(db, subject, window_days=30)
    assert 0.0 <= value <= 1.0
    assert 0.0 <= max_similarity <= 1.0

    # The August 4th event *is* in the window and is similar, so novelty < 1.
    assert value < 1.0


def test_novelty_is_one_when_nothing_precedes_it(db):
    setup_timeline(db)
    lonely = add_event(db, when=dt.datetime(2020, 1, 1, 15, 0, tzinfo=UTC))
    value, max_similarity = novelty(db, lonely, window_days=30)
    assert value == 1.0
    assert max_similarity == 0.0
