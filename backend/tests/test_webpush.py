"""Web Push over VAPID: status-code handling, retries, subscription lifecycle."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Notification, NotificationDelivery, PushSubscription, utcnow
from app.pipeline import notifications as notif
from app.pipeline.push import (
    VapidPushProvider,
    build_push_provider,
    cleanup_expired_subscriptions,
    due_retries,
    retry_failed_deliveries,
    schedule_retry,
)

UTC = dt.timezone.utc


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeWebPushException(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.response = FakeResponse(status_code) if status_code is not None else None


def provider_returning(status_code: int = 201):
    calls: list[dict] = []

    def fake_webpush(**kwargs):
        calls.append(kwargs)
        return FakeResponse(status_code)

    provider = VapidPushProvider(webpush=fake_webpush, exception_type=FakeWebPushException)
    return provider, calls


def provider_raising(status_code: int | None, message: str = "boom"):
    def fake_webpush(**kwargs):
        raise FakeWebPushException(message, status_code)

    return VapidPushProvider(webpush=fake_webpush, exception_type=FakeWebPushException)


@pytest.fixture
def configured(monkeypatch):
    """VAPID keys present, so `available` is True."""
    monkeypatch.setattr(settings, "web_push_public_key", "public-key", raising=False)
    monkeypatch.setattr(settings, "web_push_private_key", "private-key", raising=False)
    monkeypatch.setattr(settings, "web_push_subject", "mailto:test@example.invalid", raising=False)
    return settings


def make_subscription(db, user, endpoint="https://push.example.invalid/abc") -> PushSubscription:
    sub = PushSubscription(user_id=user.id, endpoint=endpoint, p256dh="p", auth="a")
    db.add(sub)
    db.commit()
    return sub


def make_notification(db, user, condition="c") -> Notification:
    outcome = notif.create_notification(
        db,
        user_id=user.id,
        notification_type="new_event",
        title="New event detected",
        body="Review analysis.",
        condition=condition,
        window="w",
    )
    db.commit()
    return outcome.notification


# --- configuration --------------------------------------------------------
def test_provider_is_unavailable_without_keys(db, user):
    provider = VapidPushProvider(webpush=lambda **kw: FakeResponse(201))
    assert provider.available is False
    status, detail = provider.send(make_subscription(db, user), {})
    assert status == "UNAVAILABLE"
    assert "not configured" in detail


def test_build_push_provider_returns_null_without_keys():
    assert build_push_provider().name == "null"


def test_build_push_provider_returns_vapid_when_configured(configured, monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.push.VapidPushProvider.available", property(lambda self: True)
    )
    assert build_push_provider().name == "vapid"


# --- status handling ------------------------------------------------------
def test_successful_send(configured, db, user):
    provider, calls = provider_returning(201)
    status, detail = provider.send(
        make_subscription(db, user), {"title": "t", "body": "b"}
    )
    assert status == "SENT"
    assert detail == "201"

    sent = calls[0]
    assert sent["subscription_info"]["endpoint"] == "https://push.example.invalid/abc"
    assert sent["vapid_private_key"] == "private-key"
    assert sent["vapid_claims"]["sub"] == "mailto:test@example.invalid"
    assert json.loads(sent["data"])["title"] == "t"


@pytest.mark.parametrize("status_code", [404, 410])
def test_gone_statuses_expire_the_subscription(configured, db, user, status_code):
    provider = provider_raising(status_code)
    status, detail = provider.send(make_subscription(db, user), {})
    assert status == "EXPIRED"
    assert str(status_code) in detail


@pytest.mark.parametrize("status_code", [408, 429, 500, 503])
def test_retryable_statuses_are_failures_marked_retryable(configured, db, user, status_code):
    status, detail = provider_raising(status_code).send(make_subscription(db, user), {})
    assert status == "FAILED"
    assert "retryable" in detail


def test_unknown_status_is_a_permanent_failure(configured, db, user):
    status, detail = provider_raising(400, "bad request").send(make_subscription(db, user), {})
    assert status == "FAILED"
    assert "retryable" not in detail


def test_a_non_http_exception_is_still_handled(configured, db, user):
    def exploding(**kwargs):
        raise RuntimeError("socket blew up")

    provider = VapidPushProvider(webpush=exploding, exception_type=FakeWebPushException)
    status, detail = provider.send(make_subscription(db, user), {})
    assert status == "FAILED"
    assert "socket blew up" in detail


# --- retry scheduling -----------------------------------------------------
def test_schedule_retry_arms_a_future_time(db, user):
    delivery = NotificationDelivery(
        notification_id=make_notification(db, user).id, channel="web_push", status="FAILED"
    )
    assert schedule_retry(delivery) is True
    assert delivery.next_retry_at > utcnow()


def test_schedule_retry_handles_an_unflushed_row(db, user):
    """retry_count is None until the DB default applies."""
    delivery = NotificationDelivery(
        notification_id=make_notification(db, user).id, channel="web_push", status="FAILED"
    )
    assert delivery.retry_count is None
    assert schedule_retry(delivery) is True


def test_schedule_retry_refuses_once_the_budget_is_spent(db, user):
    delivery = NotificationDelivery(
        notification_id=make_notification(db, user).id,
        channel="web_push",
        status="FAILED",
        retry_count=settings.web_push_max_retries,
    )
    assert schedule_retry(delivery) is False


def test_a_failed_delivery_is_armed_for_retry(db, user):
    make_subscription(db, user)
    note = make_notification(db, user)
    notif.deliver_push(
        db, note, notif.MockPushProvider(fail_endpoints={"https://push.example.invalid/abc"})
    )
    db.commit()

    delivery = db.execute(
        select(NotificationDelivery).where(NotificationDelivery.channel == "web_push")
    ).scalars().one()
    assert delivery.status == "FAILED"
    assert delivery.next_retry_at is not None


def test_due_retries_only_returns_rows_whose_time_has_come(db, user):
    sub = make_subscription(db, user)
    note = make_notification(db, user)
    delivery = NotificationDelivery(
        notification_id=note.id,
        channel="web_push",
        status="FAILED",
        subscription_id=sub.id,
        retry_count=0,
        next_retry_at=utcnow() + dt.timedelta(hours=1),
    )
    db.add(delivery)
    db.commit()
    assert due_retries(db) == []

    delivery.next_retry_at = utcnow() - dt.timedelta(minutes=1)
    db.commit()
    assert [d.id for d in due_retries(db)] == [delivery.id]


def _due_failed_delivery(db, user, sub) -> NotificationDelivery:
    note = make_notification(db, user, condition=f"c-{sub.endpoint}")
    delivery = NotificationDelivery(
        notification_id=note.id,
        channel="web_push",
        status="FAILED",
        subscription_id=sub.id,
        retry_count=0,
        next_retry_at=utcnow() - dt.timedelta(minutes=1),
    )
    db.add(delivery)
    db.commit()
    return delivery


def test_retry_sends_and_clears_the_schedule(db, user):
    sub = make_subscription(db, user)
    delivery = _due_failed_delivery(db, user, sub)

    result = retry_failed_deliveries(db, notif.MockPushProvider())
    db.refresh(delivery)
    db.refresh(sub)

    assert result["sent"] == 1
    assert delivery.status == "SENT"
    assert delivery.next_retry_at is None
    assert delivery.retry_count == 1
    assert sub.failure_count == 0


def test_retry_that_fails_again_reschedules(db, user):
    sub = make_subscription(db, user)
    delivery = _due_failed_delivery(db, user, sub)

    retry_failed_deliveries(db, notif.MockPushProvider(fail_endpoints={sub.endpoint}))
    db.refresh(delivery)

    assert delivery.status == "FAILED"
    assert delivery.retry_count == 1
    assert delivery.next_retry_at > utcnow()


def test_retry_budget_is_finite(db, user):
    """A permanently failing endpoint must not be retried forever."""
    sub = make_subscription(db, user)
    delivery = _due_failed_delivery(db, user, sub)
    provider = notif.MockPushProvider(fail_endpoints={sub.endpoint})

    attempts = 0
    # Force the row due on every pass, so only the budget can stop it.
    for _ in range(settings.web_push_max_retries + 3):
        delivery.next_retry_at = utcnow() - dt.timedelta(minutes=1)
        db.commit()
        attempts += retry_failed_deliveries(db, provider)["attempted"]
        db.refresh(delivery)

    assert attempts == settings.web_push_max_retries
    assert delivery.retry_count == settings.web_push_max_retries
    assert due_retries(db) == [], "an exhausted delivery must stop being picked up"


def test_expired_on_retry_deactivates_the_subscription(db, user):
    sub = make_subscription(db, user)
    delivery = _due_failed_delivery(db, user, sub)

    result = retry_failed_deliveries(db, notif.MockPushProvider(expire_endpoints={sub.endpoint}))
    db.refresh(sub)
    db.refresh(delivery)

    assert result["expired"] == 1
    assert sub.active is False
    assert sub.expired_at is not None
    assert delivery.next_retry_at is None


def test_repeated_failures_retire_the_subscription(db, user):
    sub = make_subscription(db, user)
    sub.failure_count = settings.web_push_max_failures - 1
    db.commit()
    _due_failed_delivery(db, user, sub)

    retry_failed_deliveries(db, notif.MockPushProvider(fail_endpoints={sub.endpoint}))
    db.refresh(sub)
    assert sub.active is False


def test_retry_skips_a_deleted_subscription(db, user):
    sub = make_subscription(db, user)
    delivery = _due_failed_delivery(db, user, sub)
    db.delete(sub)
    db.commit()

    result = retry_failed_deliveries(db, notif.MockPushProvider())
    db.refresh(delivery)
    assert result["attempted"] == 0
    assert delivery.next_retry_at is None


def test_a_raising_provider_during_retry_is_contained(db, user):
    class Exploding(notif.PushProvider):
        available = True

        def send(self, subscription, payload):
            raise RuntimeError("provider exploded")

    sub = make_subscription(db, user)
    delivery = _due_failed_delivery(db, user, sub)

    result = retry_failed_deliveries(db, Exploding())
    db.refresh(delivery)
    assert result["failed"] == 1
    assert "provider exploded" in delivery.provider_response


# --- cleanup --------------------------------------------------------------
def test_cleanup_removes_only_long_dead_subscriptions(db, user):
    recent = make_subscription(db, user, "https://push.example.invalid/recent")
    old = make_subscription(db, user, "https://push.example.invalid/old")
    active = make_subscription(db, user, "https://push.example.invalid/active")

    recent.active = False
    recent.expired_at = utcnow() - dt.timedelta(days=1)
    old.active = False
    old.expired_at = utcnow() - dt.timedelta(days=90)
    db.commit()

    assert cleanup_expired_subscriptions(db, older_than_days=30) == 1
    remaining = {s.endpoint for s in db.execute(select(PushSubscription)).scalars()}
    assert old.endpoint not in remaining
    assert recent.endpoint in remaining
    assert active.endpoint in remaining
