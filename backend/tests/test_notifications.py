"""Rule evaluation, threshold crossing and re-arming, dedup, quiet hours, digest."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from app.models import (
    AlertRule,
    Event,
    EventTicker,
    Notification,
    Signal,
    SourceHealth,
    Watchlist,
    WatchlistTicker,
)
from app.config import settings
from app.pipeline import notifications as notif

UTC = dt.timezone.utc


def make_event(db, *, text="Tariffs on chips are under review.", tickers=(("NVDA", "HIGH"),)):
    event = Event(
        source_key="mock",
        external_id="e1",
        title="Tariff statement",
        text=text,
        content_hash=f"hash-{text[:20]}-{len(tickers)}",
        event_type="tariff",
        source_timestamp=dt.datetime(2026, 3, 4, 15, 0, tzinfo=UTC),
        relevant=True,
    )
    db.add(event)
    db.flush()
    for ticker, confidence in tickers:
        db.add(EventTicker(event_id=event.id, ticker=ticker, confidence=confidence))
    db.commit()
    db.refresh(event)
    return event


def make_signal(db, event, ticker="NVDA", score=0.5, **kwargs):
    """Create or update the signal for (event, ticker).

    Updating rather than inserting mirrors the pipeline: `signals` is unique on
    (event_id, ticker), so a re-run replaces the score in place.
    """
    signal = (
        db.execute(
            select(Signal).where(Signal.event_id == event.id, Signal.ticker == ticker)
        )
        .scalars()
        .first()
    )
    if signal is None:
        signal = Signal(event_id=event.id, ticker=ticker, horizon="1d")
        db.add(signal)
    signal.score = score
    signal.label = "BULLISH" if score > 0 else "BEARISH"
    signal.confidence = kwargs.get("confidence", 0.8)
    signal.sample_size = kwargs.get("sample_size", 25)
    signal.sample_flag = kwargs.get("sample_flag", "ok")
    db.commit()
    db.refresh(signal)
    return signal


# --- idempotency ----------------------------------------------------------
def test_idempotency_key_is_stable_and_discriminating():
    base = dict(
        user_id="u", event_id="e", ticker="NVDA", notification_type="new_event",
        condition="rule:1", window="2026-03-04",
    )
    assert notif.idempotency_key(**base) == notif.idempotency_key(**base)
    assert notif.idempotency_key(**base) != notif.idempotency_key(**{**base, "ticker": "AAPL"})
    assert notif.idempotency_key(**base) != notif.idempotency_key(**{**base, "window": "2026-03-05"})


def test_duplicate_notification_is_suppressed_by_the_unique_constraint(db, user):
    kwargs = dict(
        user_id=user.id, notification_type="new_event", title="New event detected",
        body="body", condition="c", window="2026-03-04",
    )
    first = notif.create_notification(db, **kwargs)
    second = notif.create_notification(db, **kwargs)
    db.commit()

    assert first.created is True
    assert second.created is False
    assert second.suppressed_reason == "duplicate"
    assert db.execute(select(func.count(Notification.id))).scalar() == 1


def test_in_app_delivery_row_is_created_with_the_notification(db, user):
    outcome = notif.create_notification(
        db, user_id=user.id, notification_type="new_event", title="t", body="b",
        condition="c", window="w",
    )
    db.commit()
    deliveries = outcome.notification.id
    from app.models import NotificationDelivery

    rows = list(
        db.execute(
            select(NotificationDelivery).where(NotificationDelivery.notification_id == deliveries)
        ).scalars()
    )
    assert [r.channel for r in rows] == ["in_app"]
    assert rows[0].status == "SENT"


# --- quiet hours ----------------------------------------------------------
def test_quiet_hours_wrapping_midnight(db, user):
    prefs = notif.get_preferences(db, user.id)
    prefs.quiet_hours_start, prefs.quiet_hours_end = 22, 7
    prefs.timezone = "UTC"
    db.commit()

    assert notif.in_quiet_hours(prefs, dt.datetime(2026, 3, 4, 23, 0, tzinfo=UTC)) is True
    assert notif.in_quiet_hours(prefs, dt.datetime(2026, 3, 4, 3, 0, tzinfo=UTC)) is True
    assert notif.in_quiet_hours(prefs, dt.datetime(2026, 3, 4, 12, 0, tzinfo=UTC)) is False


def test_quiet_hours_within_a_day(db, user):
    prefs = notif.get_preferences(db, user.id)
    prefs.quiet_hours_start, prefs.quiet_hours_end = 9, 17
    prefs.timezone = "UTC"
    db.commit()
    assert notif.in_quiet_hours(prefs, dt.datetime(2026, 3, 4, 10, 0, tzinfo=UTC)) is True
    assert notif.in_quiet_hours(prefs, dt.datetime(2026, 3, 4, 20, 0, tzinfo=UTC)) is False


def test_quiet_hours_use_the_users_timezone(db, user):
    prefs = notif.get_preferences(db, user.id)
    prefs.quiet_hours_start, prefs.quiet_hours_end = 22, 7
    prefs.timezone = "America/New_York"
    db.commit()
    # 04:00 UTC == 23:00 ET the previous day -> quiet.
    assert notif.in_quiet_hours(prefs, dt.datetime(2026, 3, 5, 4, 0, tzinfo=UTC)) is True
    # 16:00 UTC == 11:00 ET -> not quiet.
    assert notif.in_quiet_hours(prefs, dt.datetime(2026, 3, 5, 16, 0, tzinfo=UTC)) is False


def test_quiet_hours_suppress_alerts_but_not_system_messages(db, user):
    prefs = notif.get_preferences(db, user.id)
    prefs.quiet_hours_start, prefs.quiet_hours_end = 0, 23
    prefs.timezone = "UTC"
    db.commit()

    alert = notif.create_notification(
        db, user_id=user.id, notification_type="new_event", title="t", body="b",
        condition="c", window="w",
    )
    assert alert.created is False
    assert alert.suppressed_reason == "quiet hours"

    system = notif.system_alert(
        db, user_id=user.id, condition="worker_down", title="System alert", body="worker stopped"
    )
    assert system is not None, "system alerts must never be suppressed by quiet hours"


def test_hourly_rate_limit(db, user):
    prefs = notif.get_preferences(db, user.id)
    prefs.max_per_hour = 2
    db.commit()

    for i in range(2):
        assert notif.create_notification(
            db, user_id=user.id, notification_type="new_event", title="t", body="b",
            condition=f"c{i}", window="w",
        ).created
    db.commit()

    blocked = notif.create_notification(
        db, user_id=user.id, notification_type="new_event", title="t", body="b",
        condition="c3", window="w",
    )
    assert blocked.created is False
    assert blocked.suppressed_reason == "hourly limit reached"


# --- rule evaluation ------------------------------------------------------
def test_watchlist_ticker_rule_matches(db, user):
    db.add(
        AlertRule(user_id=user.id, name="watch", rule_type="new_event", tickers=["NVDA"])
    )
    db.commit()
    event = make_event(db)
    created = notif.evaluate_event_rules(db, user_id=user.id, event=event, signals=[])
    db.commit()
    assert len(created) == 1
    assert created[0].ticker == "NVDA"
    assert created[0].title == "New event detected"


def test_rule_does_not_match_a_different_ticker(db, user):
    db.add(AlertRule(user_id=user.id, name="watch", rule_type="new_event", tickers=["AAPL"]))
    db.commit()
    event = make_event(db)
    assert notif.evaluate_event_rules(db, user_id=user.id, event=event, signals=[]) == []


def test_low_confidence_tickers_are_excluded_by_default(db, user):
    db.add(AlertRule(user_id=user.id, name="watch", rule_type="new_event", tickers=["AAPL"]))
    db.commit()
    event = make_event(db, text="an apple a day", tickers=(("AAPL", "LOW"),))
    assert notif.evaluate_event_rules(db, user_id=user.id, event=event, signals=[]) == []


def test_low_confidence_tickers_can_be_opted_into(db, user):
    db.add(
        AlertRule(
            user_id=user.id, name="watch", rule_type="new_event", tickers=["AAPL"],
            include_low_confidence_tickers=True,
        )
    )
    db.commit()
    event = make_event(db, text="an apple a day", tickers=(("AAPL", "LOW"),))
    assert len(notif.evaluate_event_rules(db, user_id=user.id, event=event, signals=[])) == 1


def test_keyword_rule_matches(db, user):
    db.add(AlertRule(user_id=user.id, name="kw", rule_type="new_event", keywords=["tariff"]))
    db.commit()
    event = make_event(db)
    created = notif.evaluate_event_rules(db, user_id=user.id, event=event, signals=[])
    assert len(created) == 1


def test_source_and_event_type_filters_apply(db, user):
    db.add(
        AlertRule(
            user_id=user.id, name="filtered", rule_type="new_event",
            keywords=["tariff"], sources=["whitehouse"],
        )
    )
    db.commit()
    event = make_event(db)  # source_key == "mock"
    assert notif.evaluate_event_rules(db, user_id=user.id, event=event, signals=[]) == []


def test_disabled_rule_never_fires(db, user):
    db.add(
        AlertRule(
            user_id=user.id, name="off", rule_type="new_event", tickers=["NVDA"], enabled=False
        )
    )
    db.commit()
    event = make_event(db)
    assert notif.evaluate_event_rules(db, user_id=user.id, event=event, signals=[]) == []


def test_re_running_rule_evaluation_creates_no_duplicates(db, user):
    db.add(AlertRule(user_id=user.id, name="watch", rule_type="new_event", tickers=["NVDA"]))
    db.commit()
    event = make_event(db)

    notif.evaluate_event_rules(db, user_id=user.id, event=event, signals=[])
    db.commit()
    again = notif.evaluate_event_rules(db, user_id=user.id, event=event, signals=[])
    db.commit()
    assert again == []
    assert db.execute(select(func.count(Notification.id))).scalar() == 1


# --- threshold crossing and re-arming ------------------------------------
def rule_with_thresholds(db, user, **kwargs):
    rule = AlertRule(
        user_id=user.id,
        name="threshold",
        rule_type="signal_threshold",
        tickers=["NVDA"],
        bullish_threshold=0.4,
        bearish_threshold=-0.4,
        **kwargs,
    )
    db.add(rule)
    db.commit()
    return rule


def test_crossing_the_bullish_threshold_notifies_once(db, user):
    rule = rule_with_thresholds(db, user)
    event = make_event(db)

    first = notif.evaluate_threshold_crossing(
        db, user_id=user.id, rule=rule, signal=make_signal(db, event, score=0.5)
    )
    db.commit()
    assert first is not None

    # Still above the threshold, but the state is disarmed: no second alert.
    second = notif.evaluate_threshold_crossing(
        db, user_id=user.id, rule=rule, signal=make_signal(db, event, score=0.55)
    )
    db.commit()
    assert second is None


def test_signal_must_return_inside_the_band_to_re_arm(db, user):
    rule = rule_with_thresholds(db, user)
    event = make_event(db)

    notif.evaluate_threshold_crossing(db, user_id=user.id, rule=rule, signal=make_signal(db, event, score=0.5))
    db.commit()

    # Back inside the band -> re-armed.
    notif.evaluate_threshold_crossing(db, user_id=user.id, rule=rule, signal=make_signal(db, event, score=0.1))
    db.commit()
    state = notif.get_signal_state(db, user.id, "NVDA", "bullish")
    assert state.armed is True

    # Crossing again now notifies.
    again = notif.evaluate_threshold_crossing(
        db, user_id=user.id, rule=rule, signal=make_signal(db, event, score=0.6)
    )
    db.commit()
    assert again is not None


def test_repeat_alerts_opt_out_of_re_arming(db, user):
    rule = rule_with_thresholds(db, user, repeat_alerts=True)
    event = make_event(db)

    notif.evaluate_threshold_crossing(db, user_id=user.id, rule=rule, signal=make_signal(db, event, score=0.5))
    db.commit()
    # A different day window so the idempotency key differs.
    signal = make_signal(db, event, score=0.55)
    signal.created_at = dt.datetime(2026, 3, 5, 15, 0, tzinfo=UTC)
    db.commit()
    assert notif.evaluate_threshold_crossing(db, user_id=user.id, rule=rule, signal=signal) is not None


def test_bearish_and_bullish_states_are_independent(db, user):
    rule = rule_with_thresholds(db, user)
    event = make_event(db)

    notif.evaluate_threshold_crossing(db, user_id=user.id, rule=rule, signal=make_signal(db, event, score=0.5))
    db.commit()
    bearish = notif.evaluate_threshold_crossing(
        db, user_id=user.id, rule=rule, signal=make_signal(db, event, score=-0.5)
    )
    db.commit()
    assert bearish is not None


def test_min_confidence_gate(db, user):
    rule = rule_with_thresholds(db, user, min_confidence=0.9)
    event = make_event(db)
    signal = make_signal(db, event, score=0.8, confidence=0.4)
    assert notif.evaluate_threshold_crossing(db, user_id=user.id, rule=rule, signal=signal) is None


def test_min_sample_size_gate(db, user):
    rule = rule_with_thresholds(db, user, min_sample_size=20)
    event = make_event(db)
    signal = make_signal(db, event, score=0.8, sample_size=3, sample_flag="unreliable")
    assert notif.evaluate_threshold_crossing(db, user_id=user.id, rule=rule, signal=signal) is None


def test_an_unreliable_sample_never_raises_an_alert(db, user):
    """A hard floor: the rule asks for nothing, and it is still excluded. Below
    min_usable_sample the score is text-only, which is not a thing to wake
    someone up for."""
    rule = rule_with_thresholds(db, user, min_sample_size=0)
    event = make_event(db)
    signal = make_signal(
        db, event, score=0.9, confidence=1.0, sample_size=4, sample_flag="unreliable"
    )
    assert notif.evaluate_threshold_crossing(db, user_id=user.id, rule=rule, signal=signal) is None


def test_the_sample_floor_is_a_floor_not_a_ceiling(db, user):
    """The same rule and score fire once the sample is usable."""
    rule = rule_with_thresholds(db, user, min_sample_size=0)
    event = make_event(db)
    signal = make_signal(
        db,
        event,
        score=0.9,
        confidence=1.0,
        sample_size=settings.min_usable_sample,
        sample_flag="limited",
    )
    assert notif.evaluate_threshold_crossing(db, user_id=user.id, rule=rule, signal=signal) is not None


def test_the_sample_floor_can_be_switched_off(db, user, monkeypatch):
    monkeypatch.setattr(settings, "alerts_require_usable_sample", False)
    rule = rule_with_thresholds(db, user, min_sample_size=0)
    event = make_event(db)
    signal = make_signal(
        db, event, score=0.9, confidence=1.0, sample_size=4, sample_flag="unreliable"
    )
    assert notif.evaluate_threshold_crossing(db, user_id=user.id, rule=rule, signal=signal) is not None


def test_threshold_body_uses_neutral_wording(db, user):
    rule = rule_with_thresholds(db, user)
    event = make_event(db)
    note = notif.evaluate_threshold_crossing(
        db, user_id=user.id, rule=rule, signal=make_signal(db, event, score=0.5)
    )
    db.commit()
    body = note.body.lower()
    for banned in ("buy now", "sell now", "guaranteed", "risk-free", "risk free"):
        assert banned not in body
    assert "review analysis" in body


# --- digest ---------------------------------------------------------------
def test_digest_contains_every_required_section(db, user):
    watchlist = Watchlist(user_id=user.id, name="Default")
    db.add(watchlist)
    db.flush()
    db.add(WatchlistTicker(watchlist_id=watchlist.id, ticker="NVDA"))
    db.add(SourceHealth(source_key="mock", status="ONLINE"))
    db.commit()

    event = make_event(db)
    make_signal(db, event, score=0.7)
    make_signal(db, event, ticker="AAPL", score=-0.6)
    notif.create_notification(
        db, user_id=user.id, notification_type="new_event", title="t", body="b",
        condition="c", window="w",
    )
    db.commit()

    digest = notif.build_digest(db, user.id)
    assert {"top_bullish", "top_bearish", "watchlist_events", "source_health",
            "notification_summary", "unresolved_failures"} <= set(digest)
    assert digest["top_bullish"][0]["ticker"] == "NVDA"
    assert digest["top_bearish"][0]["ticker"] == "AAPL"
    assert digest["notification_summary"]["total"] == 1
    assert digest["source_health"][0]["source"] == "mock"


# --- search-to-alert (Phase 3) -------------------------------------------
def test_saved_search_matches_the_same_events_as_search(db, user):
    """An alert made from a search must fire on what that search returns."""
    event = make_event(db, text="Tariffs on imported semiconductors are under review.")
    assert notif.event_matches_search(db, event, "tariffs") is True
    assert notif.event_matches_search(db, event, "golf") is False


def test_saved_search_understands_phrases_and_negation(db, user):
    """Full text, not substring: this is why the query goes through Postgres."""
    event = make_event(db, text="Tariffs on imported semiconductors are under review.")
    assert notif.event_matches_search(db, event, '"imported semiconductors"') is True
    assert notif.event_matches_search(db, event, '"semiconductors imported"') is False
    assert notif.event_matches_search(db, event, "tariffs -semiconductors") is False


def test_an_empty_saved_search_matches_nothing(db, user):
    event = make_event(db)
    assert notif.event_matches_search(db, event, "") is False
    assert notif.event_matches_search(db, event, "   ") is False


def test_a_malformed_saved_search_does_not_break_evaluation(db, user):
    event = make_event(db)
    # Must return False rather than raising, or one bad rule kills every rule.
    assert notif.event_matches_search(db, event, "((((") is False


def test_search_rule_creates_a_notification(db, user):
    db.add(
        AlertRule(
            user_id=user.id,
            name="Search: semiconductors",
            rule_type="new_event",
            keywords=["semiconductors"],
            search_query="semiconductors",
        )
    )
    db.commit()
    event = make_event(db, text="Tariffs on imported semiconductors are under review.")

    created = notif.evaluate_event_rules(db, user_id=user.id, event=event, signals=[])
    db.commit()
    assert len(created) == 1
    assert created[0].payload["matched"].startswith("search:")


def test_search_rule_that_does_not_match_stays_quiet(db, user):
    db.add(
        AlertRule(
            user_id=user.id,
            name="Search: golf",
            rule_type="new_event",
            keywords=["tariff"],  # would match on keywords alone
            search_query="golf",  # but the saved search is the real intent
        )
    )
    db.commit()
    event = make_event(db, text="Tariffs on imported semiconductors are under review.")
    assert notif.evaluate_event_rules(db, user_id=user.id, event=event, signals=[]) == []
