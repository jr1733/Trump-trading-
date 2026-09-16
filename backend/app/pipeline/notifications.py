"""Notification rules, deduplication, quiet hours and delivery.

Ordering guarantee from the spec: **rules are evaluated only after the event is
stored with a stable ID.** The event ID is part of every idempotency key, so a
re-run of the worker regenerates identical keys and the unique constraint turns
the duplicate insert into a no-op.

Wording is neutral by construction -- the phrases live in this module and there
is no code path that emits "buy", "sell", "guaranteed" or "risk-free".
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import (
    AlertRule,
    Event,
    Notification,
    NotificationDelivery,
    NotificationPreference,
    PushSubscription,
    Signal,
    SignalState,
    WatchlistTicker,
    utcnow,
)

log = logging.getLogger(__name__)

NEUTRAL_TITLES = {
    "new_event": "New event detected",
    "signal_threshold": "Signal threshold crossed",
    "similar_topic": "Similar event detected",
    "system": "System alert",
    "digest": "Daily digest",
}


def idempotency_key(
    *,
    user_id: str,
    event_id: str | None,
    ticker: str | None,
    notification_type: str,
    condition: str,
    window: str,
) -> str:
    """hash(user, event, ticker, type, threshold condition, time window).

    The `window` component is what makes "same alert, later day" a new
    notification while "same alert, same batch" is a duplicate.
    """
    raw = "|".join([user_id, event_id or "-", ticker or "-", notification_type, condition, window])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


def hour_window(moment: dt.datetime | None = None) -> str:
    return (moment or utcnow()).strftime("%Y-%m-%dT%H")


def day_window(moment: dt.datetime | None = None) -> str:
    return (moment or utcnow()).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# Preferences: quiet hours and rate limiting
# --------------------------------------------------------------------------
def get_preferences(db: Session, user_id: str) -> NotificationPreference:
    row = (
        db.execute(select(NotificationPreference).where(NotificationPreference.user_id == user_id))
        .scalars()
        .first()
    )
    if row is None:
        row = NotificationPreference(user_id=user_id)
        db.add(row)
        db.flush()
    return row


def in_quiet_hours(prefs: NotificationPreference, moment: dt.datetime | None = None) -> bool:
    """Quiet hours are expressed in the user's local timezone, inclusive of start,
    exclusive of end, and may wrap past midnight (22 -> 7)."""
    if prefs.quiet_hours_start is None or prefs.quiet_hours_end is None:
        return False
    if prefs.quiet_hours_start == prefs.quiet_hours_end:
        return False
    try:
        tz = ZoneInfo(prefs.timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    hour = (moment or utcnow()).astimezone(tz).hour
    start, end = prefs.quiet_hours_start, prefs.quiet_hours_end
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def recent_count(db: Session, user_id: str, *, minutes: int = 60) -> int:
    since = utcnow() - dt.timedelta(minutes=minutes)
    return int(
        db.execute(
            select(func.count(Notification.id)).where(
                Notification.user_id == user_id,
                Notification.created_at >= since,
                Notification.notification_type != "system",
            )
        ).scalar()
        or 0
    )


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------
@dataclass
class CreatedNotification:
    notification: Notification | None
    created: bool
    suppressed_reason: str | None = None


def create_notification(
    db: Session,
    *,
    user_id: str,
    notification_type: str,
    title: str,
    body: str,
    condition: str,
    window: str,
    severity: str = "info",
    event_id: str | None = None,
    ticker: str | None = None,
    rule_id: str | None = None,
    payload: dict | None = None,
    respect_quiet_hours: bool = True,
    respect_rate_limit: bool = True,
) -> CreatedNotification:
    """Insert one notification, idempotently.

    Returns `created=False` when the idempotency key already exists -- this is
    the normal, expected outcome of re-running the worker, not an error.
    """
    prefs = get_preferences(db, user_id)
    if not prefs.enabled and notification_type != "system":
        return CreatedNotification(None, False, "notifications disabled")

    # System alerts ignore quiet hours and rate limits: "the worker stopped" is
    # exactly the message you do not want suppressed.
    if notification_type != "system":
        if respect_quiet_hours and in_quiet_hours(prefs):
            return CreatedNotification(None, False, "quiet hours")
        if respect_rate_limit and recent_count(db, user_id) >= prefs.max_per_hour:
            return CreatedNotification(None, False, "hourly limit reached")

    key = idempotency_key(
        user_id=user_id,
        event_id=event_id,
        ticker=ticker,
        notification_type=notification_type,
        condition=condition,
        window=window,
    )
    stmt = (
        insert(Notification)
        .values(
            user_id=user_id,
            idempotency_key=key,
            notification_type=notification_type,
            severity=severity,
            title=title,
            body=body,
            event_id=event_id,
            ticker=ticker,
            rule_id=rule_id,
            payload=payload or {},
            created_at=utcnow(),
        )
        .on_conflict_do_nothing(constraint="uq_notifications_idempotency")
        .returning(Notification.id)
    )
    new_id = db.execute(stmt).scalars().first()
    db.flush()
    if new_id is None:
        return CreatedNotification(None, False, "duplicate")

    notification = db.get(Notification, new_id)
    # The in-app channel is the always-on fallback and is recorded as delivered
    # the moment the row exists.
    db.add(
        NotificationDelivery(
            notification_id=new_id,
            channel="in_app",
            status="SENT",
            sent_at=utcnow(),
        )
    )
    db.flush()
    return CreatedNotification(notification, True)


# --------------------------------------------------------------------------
# Rule evaluation
# --------------------------------------------------------------------------
def _matches_new_event_rule(rule: AlertRule, event: Event, tickers: list[str]) -> str | None:
    """Returns the matched condition string, or None."""
    blob = f"{event.title or ''} {event.text}".lower()
    if rule.sources and event.source_key not in rule.sources:
        return None
    if rule.event_types and event.event_type not in rule.event_types:
        return None

    if rule.tickers:
        hit = next((t for t in rule.tickers if t.upper() in tickers), None)
        if hit:
            return f"ticker:{hit.upper()}"
        return None
    if rule.keywords:
        hit = next((k for k in rule.keywords if k.lower() in blob), None)
        if hit:
            return f"keyword:{hit.lower()}"
        return None
    if rule.entities:
        hit = next((e for e in rule.entities if e.lower() in blob), None)
        if hit:
            return f"entity:{hit.lower()}"
        return None
    # A rule with only source/event_type filters matches anything passing them.
    if rule.sources or rule.event_types:
        return f"source:{event.source_key}"
    return None


def watchlist_tickers(db: Session, user_id: str) -> set[str]:
    from ..models import Watchlist

    rows = db.execute(
        select(WatchlistTicker.ticker)
        .join(Watchlist, Watchlist.id == WatchlistTicker.watchlist_id)
        .where(Watchlist.user_id == user_id)
    ).scalars()
    return {t.upper() for t in rows}


def evaluate_event_rules(
    db: Session, *, user_id: str, event: Event, signals: list[Signal]
) -> list[Notification]:
    """Type 1 (new event matching a watchlist/rule) and type 2 (threshold crossed)."""
    created: list[Notification] = []
    rules = list(
        db.execute(
            select(AlertRule).where(AlertRule.user_id == user_id, AlertRule.enabled.is_(True))
        ).scalars()
    )
    event_tickers = [t.ticker for t in event.tickers]
    usable_tickers = [t.ticker for t in event.tickers if t.confidence in ("HIGH", "MEDIUM")]
    watched = watchlist_tickers(db, user_id)
    window = day_window(event.source_timestamp)

    for rule in rules:
        if rule.rule_type == "new_event":
            tickers_for_rule = (
                event_tickers if rule.include_low_confidence_tickers else usable_tickers
            )
            condition = _matches_new_event_rule(rule, event, [t.upper() for t in tickers_for_rule])
            if condition is None:
                continue
            ticker = condition.split(":", 1)[1] if condition.startswith("ticker:") else None
            outcome = create_notification(
                db,
                user_id=user_id,
                notification_type="new_event",
                title=NEUTRAL_TITLES["new_event"],
                body=_event_body(event, ticker),
                condition=f"rule:{rule.id}:{condition}",
                window=window,
                event_id=event.id,
                ticker=ticker,
                rule_id=rule.id,
                payload={"rule_name": rule.name, "matched": condition},
            )
            if outcome.created and outcome.notification:
                created.append(outcome.notification)

        elif rule.rule_type == "signal_threshold":
            for signal in signals:
                if rule.tickers and signal.ticker.upper() not in [
                    t.upper() for t in rule.tickers
                ]:
                    continue
                if not rule.tickers and signal.ticker.upper() not in watched:
                    continue
                note = evaluate_threshold_crossing(
                    db, user_id=user_id, rule=rule, signal=signal, event=event
                )
                if note:
                    created.append(note)
    return created


def _event_body(event: Event, ticker: str | None) -> str:
    excerpt = (event.title or event.text or "").strip().rstrip(".")
    if len(excerpt) > 180:
        excerpt = excerpt[:177] + "..."
    suffix = f"Review analysis for {ticker}." if ticker else "Review analysis."
    return f"{event.source_key}: {excerpt}. {suffix}"


def get_signal_state(db: Session, user_id: str, ticker: str, direction: str) -> SignalState:
    row = (
        db.execute(
            select(SignalState).where(
                SignalState.user_id == user_id,
                SignalState.ticker == ticker.upper(),
                SignalState.direction == direction,
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        row = SignalState(user_id=user_id, ticker=ticker.upper(), direction=direction, armed=True)
        db.add(row)
        db.flush()
    return row


def evaluate_threshold_crossing(
    db: Session, *, user_id: str, rule: AlertRule, signal: Signal, event: Event | None = None
) -> Notification | None:
    """Notify on *crossing* only, then re-arm once the signal comes back inside.

    `repeat_alerts` on the rule opts out of the re-arm requirement.
    """
    if signal.confidence < rule.min_confidence:
        return None
    if signal.sample_size < rule.min_sample_size:
        return None

    bull = rule.bullish_threshold
    bear = rule.bearish_threshold
    direction: str | None = None
    threshold: float | None = None
    if bull is not None and signal.score >= bull:
        direction, threshold = "bullish", bull
    elif bear is not None and signal.score <= bear:
        direction, threshold = "bearish", bear

    if direction is None:
        # Inside the band: re-arm both directions for this ticker, and advance
        # the cycle so the next crossing is a new notification rather than a
        # duplicate of the previous one.
        for candidate in ("bullish", "bearish"):
            state = get_signal_state(db, user_id, signal.ticker, candidate)
            if not state.armed:
                state.cycle += 1
            state.armed = True
            state.last_score = signal.score
        db.flush()
        return None

    state = get_signal_state(db, user_id, signal.ticker, direction)
    if not state.armed and not rule.repeat_alerts:
        state.last_score = signal.score
        db.flush()
        return None

    outcome = create_notification(
        db,
        user_id=user_id,
        notification_type="signal_threshold",
        title=NEUTRAL_TITLES["signal_threshold"],
        body=(
            f"{signal.ticker}: signal {signal.score:+.2f} ({signal.label}) crossed the "
            f"{direction} threshold of {threshold:+.2f} at the {signal.horizon} horizon "
            f"(N={signal.sample_size}, {signal.sample_flag}). Review analysis."
        ),
        condition=f"rule:{rule.id}:{direction}:{threshold}",
        window=f"{day_window(signal.created_at)}:cycle{state.cycle}",
        event_id=signal.event_id,
        ticker=signal.ticker,
        rule_id=rule.id,
        severity="info",
        payload={
            "score": signal.score,
            "label": signal.label,
            "threshold": threshold,
            "direction": direction,
            "sample_size": signal.sample_size,
            "sample_flag": signal.sample_flag,
        },
    )
    state.armed = False
    state.last_score = signal.score
    state.last_crossed_at = utcnow()
    db.flush()
    return outcome.notification if outcome.created else None


def system_alert(
    db: Session,
    *,
    user_id: str,
    condition: str,
    title: str,
    body: str,
    severity: str = "warning",
    window: str | None = None,
) -> Notification | None:
    outcome = create_notification(
        db,
        user_id=user_id,
        notification_type="system",
        title=title,
        body=body,
        condition=condition,
        window=window or hour_window(),
        severity=severity,
        respect_quiet_hours=False,
        respect_rate_limit=False,
    )
    return outcome.notification if outcome.created else None


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------
class PushProvider:
    """Interface. Phase 2 supplies a real VAPID implementation."""

    name = "null"
    available = False

    def send(self, subscription: PushSubscription, payload: dict) -> tuple[str, str]:
        """Returns (status, provider_response)."""
        return "UNAVAILABLE", "web push is not configured (Phase 2)"


class MockPushProvider(PushProvider):
    """Used by tests and the E2E flow: records a delivery attempt without network."""

    name = "mock"
    available = True

    def __init__(self, fail_endpoints: set[str] | None = None, expire_endpoints: set[str] | None = None):
        self.fail_endpoints = fail_endpoints or set()
        self.expire_endpoints = expire_endpoints or set()
        self.sent: list[dict] = []

    def send(self, subscription: PushSubscription, payload: dict) -> tuple[str, str]:
        if subscription.endpoint in self.expire_endpoints:
            return "EXPIRED", "410 Gone"
        if subscription.endpoint in self.fail_endpoints:
            return "FAILED", "500 Internal Server Error"
        self.sent.append({"endpoint": subscription.endpoint, "payload": payload})
        return "SENT", "201 Created"


def deliver_push(
    db: Session, notification: Notification, provider: PushProvider | None = None
) -> list[NotificationDelivery]:
    """Attempt push delivery. Never raises -- notification failures never block
    event processing."""
    provider = provider or PushProvider()
    subs = list(
        db.execute(
            select(PushSubscription).where(
                PushSubscription.user_id == notification.user_id,
                PushSubscription.active.is_(True),
            )
        ).scalars()
    )
    if not subs:
        delivery = NotificationDelivery(
            notification_id=notification.id,
            channel="web_push",
            status="UNAVAILABLE",
            provider_response="no active push subscriptions",
        )
        db.add(delivery)
        db.flush()
        return [delivery]

    payload = {
        "title": notification.title,
        "body": notification.body,
        "notification_id": notification.id,
        "event_id": notification.event_id,
        "ticker": notification.ticker,
        "type": notification.notification_type,
    }
    deliveries: list[NotificationDelivery] = []
    for sub in subs:
        try:
            status, response = provider.send(sub, payload)
        except Exception as exc:  # a broken provider must not break the pipeline
            log.warning("push provider raised: %s", exc)
            status, response = "FAILED", f"{type(exc).__name__}: {exc}"

        delivery = NotificationDelivery(
            notification_id=notification.id,
            channel="web_push",
            status=status,
            provider_response=response[:2000],
            subscription_id=sub.id,
            sent_at=utcnow() if status == "SENT" else None,
            failed_at=utcnow() if status in ("FAILED", "EXPIRED") else None,
        )
        db.add(delivery)
        deliveries.append(delivery)

        if status == "EXPIRED":
            # 404/410 from the push service means the subscription is dead.
            sub.active = False
            sub.expired_at = utcnow()
        elif status == "FAILED":
            sub.failure_count += 1
        else:
            sub.last_used_at = utcnow()
            sub.failure_count = 0
    db.flush()
    return deliveries


def build_digest(db: Session, user_id: str, *, hours: int = 24) -> dict:
    """Contents per spec: top bullish/bearish signals, watchlist events, source
    health, notification summary, unresolved failures."""
    from ..models import SourceHealth

    since = utcnow() - dt.timedelta(hours=hours)
    signals = list(
        db.execute(
            select(Signal).where(Signal.created_at >= since).order_by(Signal.score.desc())
        ).scalars()
    )
    watched = watchlist_tickers(db, user_id)
    events = list(
        db.execute(
            select(Event)
            .join(Event.tickers)
            .where(Event.source_timestamp >= since)
            .order_by(Event.source_timestamp.desc())
            .limit(50)
        ).scalars().unique()
    )
    health = list(db.execute(select(SourceHealth)).scalars())
    notifications = list(
        db.execute(
            select(Notification).where(
                Notification.user_id == user_id, Notification.created_at >= since
            )
        ).scalars()
    )
    failed = list(
        db.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.status.in_(["FAILED", "EXPIRED"]),
                NotificationDelivery.created_at >= since,
            )
        ).scalars()
    )

    return {
        "window_hours": hours,
        "generated_at": utcnow().isoformat(),
        "top_bullish": [
            {"ticker": s.ticker, "score": s.score, "label": s.label, "n": s.sample_size}
            for s in signals[:5]
            if s.score > 0
        ],
        "top_bearish": [
            {"ticker": s.ticker, "score": s.score, "label": s.label, "n": s.sample_size}
            for s in sorted(signals, key=lambda s: s.score)[:5]
            if s.score < 0
        ],
        "watchlist_events": [
            {
                "event_id": e.id,
                "title": e.title or e.text[:100],
                "tickers": [t.ticker for t in e.tickers if t.ticker in watched],
                "source_timestamp": e.source_timestamp.isoformat(),
            }
            for e in events
            if watched.intersection({t.ticker for t in e.tickers})
        ][:10],
        "source_health": [
            {
                "source": h.source_key,
                "status": h.status,
                "consecutive_failures": h.consecutive_failures,
                "last_success_at": h.last_success_at.isoformat() if h.last_success_at else None,
            }
            for h in health
        ],
        "notification_summary": {
            "total": len(notifications),
            "unread": sum(1 for n in notifications if n.read_at is None),
            "by_type": {
                t: sum(1 for n in notifications if n.notification_type == t)
                for t in {n.notification_type for n in notifications}
            },
        },
        "unresolved_failures": [
            {"channel": d.channel, "status": d.status, "response": d.provider_response}
            for d in failed[:10]
        ],
    }
