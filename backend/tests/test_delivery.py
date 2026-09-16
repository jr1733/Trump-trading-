"""Push subscription lifecycle, expiry cleanup, email failure, and fallbacks."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.models import Notification, NotificationDelivery, PushSubscription
from app.pipeline import notifications as notif
from app.pipeline.email import EmailSender

UTC = dt.timezone.utc


def make_notification(db, user) -> Notification:
    outcome = notif.create_notification(
        db,
        user_id=user.id,
        notification_type="new_event",
        title="New event detected",
        body="Review analysis.",
        condition="c",
        window="w",
    )
    db.commit()
    return outcome.notification


def subscribe(db, user, endpoint="https://push.example.invalid/abc") -> PushSubscription:
    sub = PushSubscription(
        user_id=user.id, endpoint=endpoint, p256dh="key", auth="auth"
    )
    db.add(sub)
    db.commit()
    return sub


# --- push -----------------------------------------------------------------
def test_push_unavailable_falls_back_to_in_app(db, user):
    """No push configured: the in-app copy still exists and the attempt is logged."""
    note = make_notification(db, user)
    deliveries = notif.deliver_push(db, note)
    db.commit()

    assert [d.status for d in deliveries] == ["UNAVAILABLE"]
    in_app = list(
        db.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.notification_id == note.id,
                NotificationDelivery.channel == "in_app",
            )
        ).scalars()
    )
    assert len(in_app) == 1 and in_app[0].status == "SENT"


def test_mock_push_records_a_successful_delivery(db, user):
    note = make_notification(db, user)
    sub = subscribe(db, user)
    provider = notif.MockPushProvider()

    deliveries = notif.deliver_push(db, note, provider)
    db.commit()

    assert [d.status for d in deliveries] == ["SENT"]
    assert deliveries[0].sent_at is not None
    assert deliveries[0].subscription_id == sub.id
    assert provider.sent[0]["payload"]["notification_id"] == note.id


def test_expired_subscription_is_deactivated(db, user):
    """HTTP 404/410 from the push service means the subscription is dead."""
    note = make_notification(db, user)
    sub = subscribe(db, user, "https://push.example.invalid/gone")
    provider = notif.MockPushProvider(expire_endpoints={sub.endpoint})

    deliveries = notif.deliver_push(db, note, provider)
    db.commit()
    db.refresh(sub)

    assert deliveries[0].status == "EXPIRED"
    assert sub.active is False
    assert sub.expired_at is not None


def test_expired_subscription_is_not_used_again(db, user):
    note = make_notification(db, user)
    sub = subscribe(db, user, "https://push.example.invalid/gone")
    notif.deliver_push(db, note, notif.MockPushProvider(expire_endpoints={sub.endpoint}))
    db.commit()

    second = notif.create_notification(
        db, user_id=user.id, notification_type="new_event", title="t", body="b",
        condition="c2", window="w",
    )
    db.commit()
    deliveries = notif.deliver_push(db, second.notification, notif.MockPushProvider())
    db.commit()
    assert [d.status for d in deliveries] == ["UNAVAILABLE"]


def test_failed_push_increments_the_failure_count_but_keeps_the_subscription(db, user):
    note = make_notification(db, user)
    sub = subscribe(db, user, "https://push.example.invalid/flaky")
    notif.deliver_push(db, note, notif.MockPushProvider(fail_endpoints={sub.endpoint}))
    db.commit()
    db.refresh(sub)

    assert sub.active is True, "a 500 is transient; do not discard the subscription"
    assert sub.failure_count == 1


def test_a_raising_push_provider_does_not_break_processing(db, user):
    class Exploding(notif.PushProvider):
        available = True

        def send(self, subscription, payload):
            raise RuntimeError("provider exploded")

    note = make_notification(db, user)
    subscribe(db, user)
    deliveries = notif.deliver_push(db, note, Exploding())
    db.commit()

    assert deliveries[0].status == "FAILED"
    assert "provider exploded" in deliveries[0].provider_response


def test_multiple_subscriptions_each_get_a_delivery_row(db, user):
    note = make_notification(db, user)
    subscribe(db, user, "https://push.example.invalid/one")
    subscribe(db, user, "https://push.example.invalid/two")
    deliveries = notif.deliver_push(db, note, notif.MockPushProvider())
    db.commit()
    assert len(deliveries) == 2


# --- email ----------------------------------------------------------------
def test_email_is_disabled_when_smtp_is_not_configured(db, user):
    sender = EmailSender()
    assert sender.enabled is False

    entry = sender.send(
        db, user_id=user.id, to_address="a@example.invalid", subject="s", body="b"
    )
    db.commit()
    assert entry.status == "DISABLED"
    assert "not configured" in entry.error


def test_email_failure_is_logged_not_raised(db, user):
    class BrokenTransport:
        def send_message(self, message):
            raise OSError("connection refused")

    note = make_notification(db, user)
    entry = EmailSender(transport=BrokenTransport()).send(
        db,
        user_id=user.id,
        to_address="a@example.invalid",
        subject="Digest",
        body="body",
        notification_id=note.id,
    )
    db.commit()

    assert entry.status == "FAILED"
    assert "connection refused" in entry.error

    delivery = list(
        db.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.notification_id == note.id,
                NotificationDelivery.channel == "email",
            )
        ).scalars()
    )
    assert delivery[0].status == "FAILED"
    assert delivery[0].failed_at is not None


def test_email_success_is_logged(db, user):
    class Transport:
        def __init__(self):
            self.sent = []

        def send_message(self, message):
            self.sent.append(message)

    transport = Transport()
    entry = EmailSender(transport=transport).send(
        db, user_id=user.id, to_address="a@example.invalid", subject="Digest", body="body"
    )
    db.commit()
    assert entry.status == "SENT"
    assert len(transport.sent) == 1


def test_email_without_a_destination_is_a_recorded_failure(db, user):
    class Transport:
        def send_message(self, message):
            raise AssertionError("must not be called")

    entry = EmailSender(transport=Transport()).send(
        db, user_id=user.id, to_address=None, subject="s", body="b"
    )
    db.commit()
    assert entry.status == "FAILED"
    assert "no destination" in entry.error
