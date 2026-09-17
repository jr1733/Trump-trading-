"""Digests: scheduling in the user's timezone, idempotency, channels."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models import EmailDeliveryLog, Notification, NotificationDelivery, SourceHealth, User
from app.pipeline import digest as digest_mod
from app.pipeline import notifications as notif
from app.pipeline.email import EmailSender

UTC = dt.timezone.utc


class CollectingTransport:
    def __init__(self) -> None:
        self.sent: list = []

    def send_message(self, message) -> None:
        self.sent.append(message)


@pytest.fixture
def prefs(db, user):
    row = notif.get_preferences(db, user.id)
    row.timezone = "America/New_York"
    row.digest_hour_local = 7
    row.digest_enabled = True
    db.commit()
    return row


# --- scheduling -----------------------------------------------------------
def test_digest_is_due_at_the_local_hour(db, user, prefs):
    # 12:00 UTC == 07:00 America/New_York in summer.
    assert digest_mod.is_digest_due(prefs, moment=dt.datetime(2026, 7, 1, 11, 5, tzinfo=UTC))
    assert not digest_mod.is_digest_due(prefs, moment=dt.datetime(2026, 7, 1, 15, 5, tzinfo=UTC))


def test_digest_hour_follows_the_users_timezone(db, user, prefs):
    """The same UTC instant is due for one timezone and not another."""
    moment = dt.datetime(2026, 7, 1, 11, 5, tzinfo=UTC)
    assert digest_mod.is_digest_due(prefs, moment=moment)

    prefs.timezone = "Europe/London"
    db.commit()
    assert not digest_mod.is_digest_due(prefs, moment=moment)


def test_a_disabled_digest_is_never_due(db, user, prefs):
    prefs.digest_enabled = False
    db.commit()
    assert not digest_mod.is_digest_due(prefs, moment=dt.datetime(2026, 7, 1, 11, 5, tzinfo=UTC))


def test_local_now_falls_back_on_a_bad_timezone():
    assert digest_mod.local_now("Mars/Olympus_Mons") is not None


# --- content --------------------------------------------------------------
def test_digest_text_contains_every_section(db, user, prefs):
    db.add(SourceHealth(source_key="mock", status="ONLINE"))
    db.commit()

    digest = notif.build_digest(db, user.id)
    text = digest_mod.render_digest_text(digest)
    for heading in (
        "Top bullish signals",
        "Top bearish signals",
        "Watchlist events",
        "Source health",
        "Notifications",
        "Unresolved delivery failures",
    ):
        assert heading in text
    assert "not causation" in text


def test_digest_text_uses_no_trading_language(db, user, prefs):
    digest = notif.build_digest(db, user.id)
    blob = digest_mod.render_digest_text(digest).lower()
    for phrase in ("buy now", "sell now", "guaranteed", "risk-free", "risk free"):
        assert phrase not in blob


def test_summary_line_mentions_degraded_sources(db, user, prefs):
    db.add(SourceHealth(source_key="whitehouse", status="ERROR", consecutive_failures=4))
    db.commit()
    digest = notif.build_digest(db, user.id)
    assert "whitehouse" in digest_mod.digest_summary_line(digest)


def test_summary_line_ignores_configuration_states(db, user, prefs):
    """"Needs a key" is not a degraded source; it is an unconfigured one."""
    db.add(SourceHealth(source_key="congress", status="NEEDS_KEY"))
    db.add(SourceHealth(source_key="truth_social", status="MANUAL_ONLY"))
    db.commit()
    line = digest_mod.digest_summary_line(notif.build_digest(db, user.id))
    assert "congress" not in line and "truth_social" not in line


# --- delivery -------------------------------------------------------------
def test_send_digest_creates_a_notification(db, user, prefs):
    result = digest_mod.send_digest(db, user)
    assert result["created"] is True

    row = db.execute(
        select(Notification).where(Notification.notification_type == "digest")
    ).scalars().one()
    assert row.title == "Daily digest"
    assert row.payload["window_hours"] == 24


def test_send_digest_is_idempotent_within_the_day(db, user, prefs):
    first = digest_mod.send_digest(db, user)
    second = digest_mod.send_digest(db, user)

    assert first["created"] is True
    assert second["created"] is False and second["reason"] == "duplicate"
    assert db.execute(select(func.count(Notification.id))).scalar() == 1


def test_a_new_local_day_sends_again(db, user, prefs):
    digest_mod.send_digest(db, user, moment=dt.datetime(2026, 7, 1, 11, 0, tzinfo=UTC))
    second = digest_mod.send_digest(db, user, moment=dt.datetime(2026, 7, 2, 11, 0, tzinfo=UTC))
    assert second["created"] is True
    assert db.execute(select(func.count(Notification.id))).scalar() == 2


def test_daily_and_hourly_do_not_collide(db, user, prefs):
    daily = digest_mod.send_digest(db, user, cadence="daily")
    hourly = digest_mod.send_digest(db, user, cadence="hourly", hours=1)
    assert daily["created"] and hourly["created"]


def test_quiet_hours_do_not_suppress_a_digest(db, user, prefs):
    """A 07:00 digest is something the user asked for, not an interruption."""
    prefs.quiet_hours_start, prefs.quiet_hours_end = 0, 23
    db.commit()
    assert digest_mod.send_digest(db, user)["created"] is True


def test_rate_limit_does_not_suppress_a_digest(db, user, prefs):
    prefs.max_per_hour = 1
    db.commit()
    notif.create_notification(
        db, user_id=user.id, notification_type="new_event", title="t", body="b",
        condition="c", window="w",
    )
    db.commit()
    assert digest_mod.send_digest(db, user)["created"] is True


def test_email_channel_sends_and_logs(db, user, prefs):
    user.email = "operator@example.invalid"
    prefs.channels = ["in_app", "email"]
    db.commit()

    transport = CollectingTransport()
    result = digest_mod.send_digest(db, user, sender=EmailSender(transport=transport))

    assert result["email"] == "SENT"
    assert len(transport.sent) == 1
    assert db.execute(select(func.count(EmailDeliveryLog.id))).scalar() == 1


def test_email_failure_does_not_lose_the_digest(db, user, prefs):
    class BrokenTransport:
        def send_message(self, message):
            raise OSError("connection refused")

    user.email = "operator@example.invalid"
    prefs.channels = ["in_app", "email"]
    db.commit()

    result = digest_mod.send_digest(db, user, sender=EmailSender(transport=BrokenTransport()))
    assert result["email"] == "FAILED"
    # The in-app copy is the system of record and must survive.
    assert db.execute(
        select(func.count(Notification.id)).where(Notification.notification_type == "digest")
    ).scalar() == 1


def test_email_is_skipped_when_not_a_chosen_channel(db, user, prefs):
    prefs.channels = ["in_app"]
    db.commit()
    transport = CollectingTransport()
    result = digest_mod.send_digest(db, user, sender=EmailSender(transport=transport))
    assert result["email"] is None
    assert transport.sent == []


def test_push_channel_records_a_delivery(db, user, prefs):
    prefs.channels = ["in_app", "web_push"]
    db.commit()
    result = digest_mod.send_digest(db, user, push_provider=notif.MockPushProvider())
    assert result["push"] == ["UNAVAILABLE"]  # no subscriptions registered

    rows = list(
        db.execute(
            select(NotificationDelivery).where(NotificationDelivery.channel == "web_push")
        ).scalars()
    )
    assert len(rows) == 1


# --- the worker entry point ----------------------------------------------
def test_run_due_digests_only_sends_at_the_right_hour(db, user, prefs):
    off_hour = digest_mod.run_due_digests(db, moment=dt.datetime(2026, 7, 1, 20, 0, tzinfo=UTC))
    assert off_hour["sent"] == 0

    on_hour = digest_mod.run_due_digests(db, moment=dt.datetime(2026, 7, 1, 11, 0, tzinfo=UTC))
    assert on_hour["sent"] == 1


def test_run_due_digests_is_idempotent(db, user, prefs):
    moment = dt.datetime(2026, 7, 1, 11, 0, tzinfo=UTC)
    digest_mod.run_due_digests(db, moment=moment)
    again = digest_mod.run_due_digests(db, moment=moment)
    assert again["sent"] == 0 and again["skipped"] == 1


def test_run_due_digests_covers_every_user(db, prefs, user):
    second = User(display_name="Second", timezone="America/New_York")
    db.add(second)
    db.commit()
    other = notif.get_preferences(db, second.id)
    other.timezone = "America/New_York"
    other.digest_hour_local = 7
    db.commit()

    summary = digest_mod.run_due_digests(db, moment=dt.datetime(2026, 7, 1, 11, 0, tzinfo=UTC))
    assert summary["checked"] == 2
    assert summary["sent"] == 2


def test_hourly_digests_are_opt_in(db, user, prefs, monkeypatch):
    monkeypatch.setattr(settings, "hourly_digest_enabled", False, raising=False)
    off = digest_mod.run_due_digests(db, moment=dt.datetime(2026, 7, 1, 20, 0, tzinfo=UTC))
    assert off["sent"] == 0

    monkeypatch.setattr(settings, "hourly_digest_enabled", True, raising=False)
    on = digest_mod.run_due_digests(db, moment=dt.datetime(2026, 7, 1, 20, 0, tzinfo=UTC))
    assert on["sent"] == 1
    assert on["results"][0]["cadence"] == "hourly"
