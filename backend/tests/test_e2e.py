"""The end-to-end test from the specification, §14.

    1. Insert a mock event.
    2. Run the pipeline.
    3. Generate a signal.
    4. Match a watchlist rule.
    5. Create an in-app notification.
    6. Attempt a mock push delivery.
    7. Re-run the worker and assert no duplicate event, signal or notification.

Everything runs against the mock source, the mock market provider and the canned
analysis client -- no network, no API keys.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from app.llm.fake import CannedAnthropicClient
from app.market.service import MarketDataService
from app.models import (
    AlertRule,
    Event,
    Notification,
    NotificationDelivery,
    PushSubscription,
    RawEvent,
    Signal,
    Watchlist,
    WatchlistTicker,
)
from app.pipeline import notifications as notif
from app.pipeline.runner import run_pipeline
from app.seed import loader
from app.sources.base import RawItem
from app.sources.registry import store_raw_items

UTC = dt.timezone.utc


def counts(db) -> dict[str, int]:
    return {
        "events": db.execute(select(func.count(Event.id))).scalar(),
        "signals": db.execute(select(func.count(Signal.id))).scalar(),
        "notifications": db.execute(select(func.count(Notification.id))).scalar(),
        "deliveries": db.execute(select(func.count(NotificationDelivery.id))).scalar(),
    }


def test_end_to_end_pipeline_is_idempotent(db):
    # --- setup: reference data, user, watchlist, alert rule ---------------
    loader.seed_reference_data(db)
    user = loader.seed_user(db)

    watchlist = db.execute(select(Watchlist).where(Watchlist.user_id == user.id)).scalars().first()
    tickers = {
        row.ticker
        for row in db.execute(
            select(WatchlistTicker).where(WatchlistTicker.watchlist_id == watchlist.id)
        ).scalars()
    }
    assert "AAPL" in tickers, "the seeded watchlist should contain AAPL"

    rule = (
        db.execute(
            select(AlertRule).where(
                AlertRule.user_id == user.id, AlertRule.rule_type == "new_event"
            )
        )
        .scalars()
        .first()
    )
    assert rule is not None

    db.add(
        PushSubscription(
            user_id=user.id,
            endpoint="https://push.example.invalid/e2e",
            p256dh="key",
            auth="auth",
        )
    )
    db.commit()

    # Some history, so the historical statistics have something to work with.
    history = [
        RawItem(
            source_key="archive",
            external_id=f"hist-{i}",
            title="Tariff statement (AAPL)",
            text=(
                "Announced a review of tariffs on consumer electronics imports. "
                f"Apple Inc. and other manufacturers would be covered. (ref {i})"
            ),
            source_timestamp=dt.datetime(2025, 1, 6, 15, 0, tzinfo=UTC) + dt.timedelta(days=7 * i),
            payload={
                "text": (
                    "Announced a review of tariffs on consumer electronics imports. "
                    f"Apple Inc. and other manufacturers would be covered. (ref {i})"
                ),
                "title": "Tariff statement (AAPL)",
                "historical": True,
                "event_type": "tariff",
                "primary_ticker": "AAPL",
            },
        )
        for i in range(24)
    ]
    store_raw_items(db, history)
    db.commit()

    # --- 1. insert a mock event ------------------------------------------
    subject_text = (
        "We are looking very seriously at tariffs on consumer electronics coming in "
        "from overseas. Apple Inc. has made commitments about building here."
    )
    store_raw_items(
        db,
        [
            RawItem(
                source_key="mock",
                external_id="e2e-subject",
                title="Statement on consumer electronics tariffs",
                text=subject_text,
                author="Donald J. Trump",
                url="https://example.invalid/e2e",
                source_timestamp=dt.datetime(2026, 3, 4, 15, 30, tzinfo=UTC),
                payload={
                    "text": subject_text,
                    "title": "Statement on consumer electronics tariffs",
                    "author": "Donald J. Trump",
                    "url": "https://example.invalid/e2e",
                },
            )
        ],
    )
    db.commit()
    assert db.execute(select(func.count(RawEvent.id))).scalar() == 25

    # --- 2. run the pipeline ---------------------------------------------
    push = notif.MockPushProvider()
    report = run_pipeline(
        db,
        limit=200,
        client=CannedAnthropicClient(),
        market=MarketDataService(db),
        push_provider=push,
    )
    assert report.errors == []
    assert report.events_created == 25

    subject = (
        db.execute(select(Event).where(Event.external_id == "e2e-subject")).scalars().one()
    )
    assert subject.is_historical is False
    assert subject.relevant is True
    assert subject.analysis_status == "COMPLETE"
    assert subject.event_type == "tariff"
    assert subject.processing_timestamp is not None
    assert {t.ticker for t in subject.tickers} >= {"AAPL"}

    # --- 3. a signal was generated ---------------------------------------
    signals = list(
        db.execute(select(Signal).where(Signal.event_id == subject.id)).scalars()
    )
    assert signals, "the pipeline must produce at least one signal"
    aapl = next(s for s in signals if s.ticker == "AAPL")
    assert -1.0 <= aapl.score <= 1.0
    assert aapl.label in {
        "STRONGLY BULLISH", "BULLISH", "NEUTRAL", "BEARISH", "STRONGLY BEARISH"
    }
    # The historical sample was seeded above, so this one is not "unreliable".
    assert aapl.sample_size >= 20
    assert aapl.sample_flag == "ok"
    # The "Why?" panel is mandatory and must be populated.
    assert aapl.components and aapl.weights
    assert aapl.uncertainties["items"], "every signal must carry its uncertainties"
    assert aapl.historical_stats["stats"]["1d"]["n"] == aapl.sample_size

    # --- 4 & 5. the watchlist rule matched and created an in-app notification
    notifications = list(
        db.execute(
            select(Notification).where(Notification.event_id == subject.id)
        ).scalars()
    )
    assert notifications, "the AAPL watchlist rule should have matched"
    assert any(n.ticker == "AAPL" for n in notifications)
    assert all(n.title in {"New event detected", "Signal threshold crossed"} for n in notifications)

    in_app = list(
        db.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.notification_id.in_([n.id for n in notifications]),
                NotificationDelivery.channel == "in_app",
            )
        ).scalars()
    )
    assert in_app and all(d.status == "SENT" for d in in_app)

    # --- 6. a push delivery was attempted ---------------------------------
    push_deliveries = list(
        db.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.notification_id.in_([n.id for n in notifications]),
                NotificationDelivery.channel == "web_push",
            )
        ).scalars()
    )
    assert push_deliveries, "push delivery must at least be attempted and logged"
    assert all(d.status == "SENT" for d in push_deliveries)
    assert push.sent, "the mock push provider should have received a payload"

    before = counts(db)

    # --- 7. re-run the worker: nothing may be duplicated ------------------
    # Re-ingest the same items too, exactly as a re-poll would.
    store_raw_items(db, history)
    store_raw_items(
        db,
        [
            RawItem(
                source_key="mock",
                external_id="e2e-subject",
                title="Statement on consumer electronics tariffs",
                text=subject_text,
                source_timestamp=dt.datetime(2026, 3, 4, 15, 30, tzinfo=UTC),
                payload={"text": subject_text, "title": "Statement on consumer electronics tariffs"},
            )
        ],
    )
    db.commit()

    second = run_pipeline(
        db,
        limit=200,
        client=CannedAnthropicClient(),
        market=MarketDataService(db),
        push_provider=push,
    )
    assert second.errors == []
    assert second.events_created == 0

    after = counts(db)
    assert after["events"] == before["events"], "re-running created a duplicate event"
    assert after["signals"] == before["signals"], "re-running created a duplicate signal"
    assert after["notifications"] == before["notifications"], (
        "re-running created a duplicate notification"
    )
    assert after["deliveries"] == before["deliveries"], (
        "re-running created a duplicate delivery attempt"
    )


def test_pipeline_survives_an_unavailable_model(db):
    """No API key: events still flow, with analysis_status UNAVAILABLE."""
    from app.llm.client import AnthropicClient

    loader.seed_reference_data(db)
    loader.seed_user(db)

    text = "Tariffs on imported semiconductors are under review. Nvidia is affected."
    store_raw_items(
        db,
        [
            RawItem(
                source_key="mock",
                external_id="no-key",
                title="Chips",
                text=text,
                source_timestamp=dt.datetime(2026, 3, 4, 15, 30, tzinfo=UTC),
                payload={"text": text, "title": "Chips"},
            )
        ],
    )
    db.commit()

    report = run_pipeline(db, client=AnthropicClient(client=None), market=MarketDataService(db))
    assert report.errors == []

    event = db.execute(select(Event).where(Event.external_id == "no-key")).scalars().one()
    assert event.analysis_status == "UNAVAILABLE"
    assert {t.ticker for t in event.tickers} >= {"NVDA"}

    signals = list(db.execute(select(Signal).where(Signal.event_id == event.id)).scalars())
    assert signals, "a signal is still produced from the rule-based sentiment"
    assert signals[0].components["sentiment_source"] == "rule_based"
    assert any("rule_based" in u for u in signals[0].uncertainties["items"])


def test_irrelevant_events_are_filtered_before_any_model_call(db):
    from app.models import LLMUsage

    loader.seed_reference_data(db)
    loader.seed_user(db)

    text = "Played a great round of golf today. Beautiful weather and wonderful people."
    store_raw_items(
        db,
        [
            RawItem(
                source_key="mock",
                external_id="golf",
                title="Golf",
                text=text,
                source_timestamp=dt.datetime(2026, 3, 4, 15, 30, tzinfo=UTC),
                payload={"text": text, "title": "Golf"},
            )
        ],
    )
    db.commit()

    run_pipeline(db, client=CannedAnthropicClient(), market=MarketDataService(db))

    event = db.execute(select(Event).where(Event.external_id == "golf")).scalars().one()
    assert event.relevant is False
    assert event.analysis_status == "SKIPPED"
    assert db.execute(select(func.count(Signal.id))).scalar() == 0
    # Triage may run, but the expensive full analysis must not.
    analysis_calls = db.execute(
        select(func.count(LLMUsage.id)).where(LLMUsage.purpose == "analysis")
    ).scalar()
    assert analysis_calls == 0
