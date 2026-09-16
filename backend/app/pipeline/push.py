"""Web Push delivery over VAPID.

Behaviour the rest of the system depends on:

* **Never raises.** A push failure returns a status; it never reaches the
  pipeline. The in-app notification already exists and is the system of record.
* **404/410 retires the subscription.** Those codes mean the push service has
  permanently discarded the endpoint; retrying is pointless and keeping it alive
  would consume retry budget forever.
* **429/5xx is retryable.** The delivery row gets a `next_retry_at` with
  exponential backoff, and the worker picks it up later.
* **Everything else is a permanent failure** and is logged with the provider's
  own response, because that text is usually the only diagnostic available.

iOS reminder: none of this reaches an iPhone unless the user added the app to
the Home Screen. In a Safari tab no subscription can be created in the first
place, so there is simply nothing here to send to.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Notification, NotificationDelivery, PushSubscription, utcnow
from ..sources.http import backoff_delay
from .notifications import PushProvider

log = logging.getLogger(__name__)

# Push services use these to say "this endpoint is gone; stop sending".
GONE_STATUSES = {404, 410}
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}


class VapidPushProvider(PushProvider):
    """Real Web Push. Requires WEB_PUSH_PUBLIC_KEY / WEB_PUSH_PRIVATE_KEY."""

    name = "vapid"

    def __init__(self, webpush=None, exception_type=None) -> None:
        # Injectable so tests exercise the status-code handling without network.
        self._webpush = webpush
        self._exception_type = exception_type
        if webpush is None and settings.push_enabled:
            try:
                from pywebpush import WebPushException, webpush as real_webpush

                self._webpush = real_webpush
                self._exception_type = WebPushException
            except ImportError:  # pragma: no cover - depends on install
                log.error("pywebpush is not installed; web push is disabled")

    @property
    def available(self) -> bool:
        return self._webpush is not None and settings.push_enabled

    def send(self, subscription: PushSubscription, payload: dict) -> tuple[str, str]:
        if not self.available:
            return "UNAVAILABLE", "web push is not configured (no VAPID keys)"

        subscription_info = {
            "endpoint": subscription.endpoint,
            "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
        }
        claims = {"sub": settings.web_push_subject or "mailto:operator@localhost"}

        try:
            response = self._webpush(
                subscription_info=subscription_info,
                data=json.dumps(payload),
                vapid_private_key=settings.web_push_private_key,
                vapid_claims=claims,
                ttl=settings.web_push_ttl_seconds,
            )
            status = getattr(response, "status_code", 201)
            return "SENT", f"{status}"
        except Exception as exc:
            status = _status_of(exc, self._exception_type)
            detail = f"{type(exc).__name__}: {exc}"[:2000]
            if status in GONE_STATUSES:
                return "EXPIRED", f"{status} subscription gone: {detail}"
            if status in RETRYABLE_STATUSES:
                return "FAILED", f"{status} retryable: {detail}"
            log.warning("push delivery failed: %s", detail)
            return "FAILED", detail


def _status_of(exc: Exception, exception_type) -> int | None:
    """Dig the HTTP status out of a WebPushException, if there is one."""
    if exception_type is not None and not isinstance(exc, exception_type):
        return None
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def build_push_provider() -> PushProvider:
    """The provider the app should use, given the current configuration."""
    if settings.push_enabled:
        provider = VapidPushProvider()
        if provider.available:
            return provider
    # Not configured: the null provider records an UNAVAILABLE delivery row so
    # the Settings page can show *why* nothing arrived.
    return PushProvider()


# --------------------------------------------------------------------------
# Retry
# --------------------------------------------------------------------------
def schedule_retry(delivery: NotificationDelivery) -> bool:
    """Arm the next retry. Returns False when the budget is spent.

    `retry_count` is None until the row is flushed (the default is applied by
    the database), so callers may arm a retry on an unflushed delivery.
    """
    attempts = delivery.retry_count or 0
    if attempts >= settings.web_push_max_retries:
        return False
    delay = backoff_delay(attempts, base=30.0, cap=900.0)
    delivery.next_retry_at = utcnow() + dt.timedelta(seconds=delay)
    return True


def due_retries(db: Session, limit: int = 50) -> list[NotificationDelivery]:
    stmt = (
        select(NotificationDelivery)
        .where(
            NotificationDelivery.channel == "web_push",
            NotificationDelivery.status == "FAILED",
            NotificationDelivery.next_retry_at.is_not(None),
            NotificationDelivery.next_retry_at <= utcnow(),
            NotificationDelivery.retry_count < settings.web_push_max_retries,
        )
        .order_by(NotificationDelivery.next_retry_at)
        .limit(limit)
    )
    return list(db.execute(stmt).scalars())


def retry_failed_deliveries(db: Session, provider: PushProvider | None = None) -> dict:
    """Retry due push deliveries. Never raises; returns a summary."""
    provider = provider or build_push_provider()
    result = {"attempted": 0, "sent": 0, "failed": 0, "expired": 0, "exhausted": 0}

    for delivery in due_retries(db):
        notification = db.get(Notification, delivery.notification_id)
        subscription = (
            db.get(PushSubscription, delivery.subscription_id)
            if delivery.subscription_id
            else None
        )
        if notification is None or subscription is None or not subscription.active:
            # The subscription or the notification is gone: stop retrying rather
            # than leaving the row due forever.
            delivery.next_retry_at = None
            result["exhausted"] += 1
            continue

        result["attempted"] += 1
        delivery.retry_count += 1
        payload = {
            "title": notification.title,
            "body": notification.body,
            "notification_id": notification.id,
            "event_id": notification.event_id,
            "ticker": notification.ticker,
            "type": notification.notification_type,
        }
        try:
            status, response = provider.send(subscription, payload)
        except Exception as exc:  # a broken provider must not break the worker
            status, response = "FAILED", f"{type(exc).__name__}: {exc}"

        delivery.status = status
        delivery.provider_response = response[:2000]
        if status == "SENT":
            delivery.sent_at = utcnow()
            delivery.next_retry_at = None
            subscription.failure_count = 0
            subscription.last_used_at = utcnow()
            result["sent"] += 1
        elif status == "EXPIRED":
            delivery.failed_at = utcnow()
            delivery.next_retry_at = None
            subscription.active = False
            subscription.expired_at = utcnow()
            result["expired"] += 1
        else:
            delivery.failed_at = utcnow()
            subscription.failure_count += 1
            if not schedule_retry(delivery):
                delivery.next_retry_at = None
                result["exhausted"] += 1
            if subscription.failure_count >= settings.web_push_max_failures:
                # A permanently broken endpoint stops consuming retry budget.
                subscription.active = False
                subscription.expired_at = utcnow()
            result["failed"] += 1

    db.commit()
    return result


def cleanup_expired_subscriptions(db: Session, *, older_than_days: int = 30) -> int:
    """Delete subscriptions that have been inactive for a while.

    Deactivation happens immediately on a 404/410; this is the second step that
    stops the table growing without bound.
    """
    cutoff = utcnow() - dt.timedelta(days=older_than_days)
    rows = list(
        db.execute(
            select(PushSubscription).where(
                PushSubscription.active.is_(False),
                PushSubscription.expired_at.is_not(None),
                PushSubscription.expired_at < cutoff,
            )
        ).scalars()
    )
    for row in rows:
        db.delete(row)
    db.commit()
    return len(rows)
