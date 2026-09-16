"""Watchlists, alert rules, notification preferences, notifications, push."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from ..auth import current_user
from ..config import settings
from ..db import get_db
from ..models import (
    AlertRule,
    Notification,
    NotificationDelivery,
    PushSubscription,
    User,
    Watchlist,
    WatchlistTicker,
    utcnow,
)
from ..pipeline import notifications as notif
from ..schemas import AlertRuleIn, PreferencesIn, PushSubscriptionIn, WatchlistTickerIn
from .serializers import notification_out

router = APIRouter(prefix="/api", tags=["user"])


def _default_watchlist(db: Session, user: User) -> Watchlist:
    row = db.execute(select(Watchlist).where(Watchlist.user_id == user.id)).scalars().first()
    if row is None:
        row = Watchlist(user_id=user.id, name="Default")
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


# --------------------------------------------------------------------------
# Watchlist
# --------------------------------------------------------------------------
@router.get("/watchlist")
def get_watchlist(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    from ..models import Signal, Ticker

    watchlist = _default_watchlist(db, user)
    rows = list(
        db.execute(
            select(WatchlistTicker)
            .where(WatchlistTicker.watchlist_id == watchlist.id)
            .order_by(WatchlistTicker.ticker)
        ).scalars()
    )
    items = []
    for row in rows:
        latest = (
            db.execute(
                select(Signal)
                .where(Signal.ticker == row.ticker)
                .order_by(desc(Signal.created_at))
                .limit(1)
            )
            .scalars()
            .first()
        )
        meta = db.get(Ticker, row.ticker)
        items.append(
            {
                "ticker": row.ticker,
                "name": meta.name if meta else row.ticker,
                "notes": row.notes,
                "latest_signal": (
                    {
                        "score": latest.score,
                        "label": latest.label,
                        "confidence": latest.confidence,
                        "sample_size": latest.sample_size,
                        "sample_flag": latest.sample_flag,
                        "created_at": latest.created_at.isoformat(),
                    }
                    if latest
                    else None
                ),
            }
        )
    return {"name": watchlist.name, "items": items}


@router.post("/watchlist")
def add_watchlist_ticker(
    body: WatchlistTickerIn, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict:
    watchlist = _default_watchlist(db, user)
    existing = (
        db.execute(
            select(WatchlistTicker).where(
                WatchlistTicker.watchlist_id == watchlist.id,
                WatchlistTicker.ticker == body.ticker,
            )
        )
        .scalars()
        .first()
    )
    if existing is None:
        db.add(
            WatchlistTicker(watchlist_id=watchlist.id, ticker=body.ticker, notes=body.notes)
        )
        db.commit()
    return get_watchlist(db, user)


@router.delete("/watchlist/{ticker}")
def remove_watchlist_ticker(
    ticker: str, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict:
    watchlist = _default_watchlist(db, user)
    row = (
        db.execute(
            select(WatchlistTicker).where(
                WatchlistTicker.watchlist_id == watchlist.id,
                WatchlistTicker.ticker == ticker.upper(),
            )
        )
        .scalars()
        .first()
    )
    if row:
        db.delete(row)
        db.commit()
    return get_watchlist(db, user)


# --------------------------------------------------------------------------
# Alert rules
# --------------------------------------------------------------------------
def _rule_dict(rule: AlertRule) -> dict:
    return {
        "id": rule.id,
        "name": rule.name,
        "enabled": rule.enabled,
        "rule_type": rule.rule_type,
        "tickers": rule.tickers or [],
        "keywords": rule.keywords or [],
        "entities": rule.entities or [],
        "event_types": rule.event_types or [],
        "sources": rule.sources or [],
        "channels": rule.channels or ["in_app"],
        "bullish_threshold": rule.bullish_threshold,
        "bearish_threshold": rule.bearish_threshold,
        "min_confidence": rule.min_confidence,
        "min_sample_size": rule.min_sample_size,
        "include_low_confidence_tickers": rule.include_low_confidence_tickers,
        "repeat_alerts": rule.repeat_alerts,
        "max_per_hour": rule.max_per_hour,
        "grouped_delivery": rule.grouped_delivery,
        "search_query": rule.search_query,
        "created_at": rule.created_at.isoformat(),
    }


@router.get("/alert-rules")
def list_rules(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    rows = list(
        db.execute(
            select(AlertRule).where(AlertRule.user_id == user.id).order_by(AlertRule.created_at)
        ).scalars()
    )
    return {"items": [_rule_dict(r) for r in rows]}


@router.post("/alert-rules")
def create_rule(
    body: AlertRuleIn, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict:
    rule = AlertRule(user_id=user.id, **body.model_dump())
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return _rule_dict(rule)


@router.put("/alert-rules/{rule_id}")
def update_rule(
    rule_id: str,
    body: AlertRuleIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    rule = db.get(AlertRule, rule_id)
    if rule is None or rule.user_id != user.id:
        raise HTTPException(status_code=404, detail="Rule not found")
    for key, value in body.model_dump().items():
        setattr(rule, key, value)
    db.commit()
    db.refresh(rule)
    return _rule_dict(rule)


@router.delete("/alert-rules/{rule_id}")
def delete_rule(
    rule_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict:
    rule = db.get(AlertRule, rule_id)
    if rule is None or rule.user_id != user.id:
        raise HTTPException(status_code=404, detail="Rule not found")
    db.delete(rule)
    db.commit()
    return {"deleted": rule_id}


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------
@router.get("/notifications")
def list_notifications(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    limit: int = Query(50, ge=1, le=200),
    unread_only: bool = False,
    include_archived: bool = False,
) -> dict:
    stmt = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    if not include_archived:
        stmt = stmt.where(Notification.archived_at.is_(None))
    rows = list(
        db.execute(stmt.order_by(desc(Notification.created_at)).limit(limit)).scalars()
    )
    unread = int(
        db.execute(
            select(func.count(Notification.id)).where(
                Notification.user_id == user.id,
                Notification.read_at.is_(None),
                Notification.archived_at.is_(None),
            )
        ).scalar()
        or 0
    )
    return {
        "unread_count": unread,
        "items": [notification_out(db, n).model_dump() for n in rows],
    }


@router.post("/notifications/{notification_id}/read")
def mark_read(
    notification_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict:
    note = db.get(Notification, notification_id)
    if note is None or note.user_id != user.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    if note.read_at is None:
        note.read_at = utcnow()
        for delivery in db.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.notification_id == note.id,
                NotificationDelivery.channel == "in_app",
            )
        ).scalars():
            delivery.read_at = note.read_at
        db.commit()
    return {"id": note.id, "read_at": note.read_at.isoformat() if note.read_at else None}


@router.post("/notifications/read-all")
def mark_all_read(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    now = utcnow()
    rows = list(
        db.execute(
            select(Notification).where(
                Notification.user_id == user.id, Notification.read_at.is_(None)
            )
        ).scalars()
    )
    for note in rows:
        note.read_at = now
    db.commit()
    return {"marked": len(rows)}


@router.post("/notifications/{notification_id}/archive")
def archive_notification(
    notification_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict:
    note = db.get(Notification, notification_id)
    if note is None or note.user_id != user.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    note.archived_at = utcnow()
    db.commit()
    return {"id": note.id, "archived": True}


@router.delete("/notifications/{notification_id}")
def delete_notification(
    notification_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict:
    note = db.get(Notification, notification_id)
    if note is None or note.user_id != user.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    db.delete(note)
    db.commit()
    return {"deleted": notification_id}


@router.post("/notifications/test")
def send_test_notification(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict:
    """Test notification -- always lands in the notification centre, and attempts
    push so the Settings page can show whether push actually works."""
    outcome = notif.create_notification(
        db,
        user_id=user.id,
        notification_type="system",
        title="Test notification",
        body="This is a test. If you can see this in the notification centre, in-app delivery works.",
        condition="test",
        window=utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
        severity="info",
        respect_quiet_hours=False,
        respect_rate_limit=False,
    )
    deliveries = []
    if outcome.notification:
        deliveries = [
            {"channel": d.channel, "status": d.status, "response": d.provider_response}
            for d in notif.deliver_push(db, outcome.notification)
        ]
    db.commit()
    return {
        "created": outcome.created,
        "reason": outcome.suppressed_reason,
        "push_deliveries": deliveries,
    }


# --------------------------------------------------------------------------
# Preferences and push subscriptions
# --------------------------------------------------------------------------
@router.get("/preferences")
def get_preferences(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    prefs = notif.get_preferences(db, user.id)
    db.commit()
    return {
        "enabled": prefs.enabled,
        "quiet_hours_start": prefs.quiet_hours_start,
        "quiet_hours_end": prefs.quiet_hours_end,
        "timezone": prefs.timezone,
        "channels": prefs.channels or ["in_app"],
        "min_confidence": prefs.min_confidence,
        "min_sample_size": prefs.min_sample_size,
        "max_per_hour": prefs.max_per_hour,
        "digest_enabled": prefs.digest_enabled,
        "digest_hour_local": prefs.digest_hour_local,
        "in_quiet_hours_now": notif.in_quiet_hours(prefs),
    }


@router.put("/preferences")
def update_preferences(
    body: PreferencesIn, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict:
    prefs = notif.get_preferences(db, user.id)
    for key, value in body.model_dump().items():
        setattr(prefs, key, value)
    db.commit()
    return get_preferences(db, user)


@router.get("/push/status")
def push_status(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    subs = list(
        db.execute(
            select(PushSubscription).where(PushSubscription.user_id == user.id)
        ).scalars()
    )
    return {
        # Only the PUBLIC key is ever sent to the browser.
        "public_key": settings.web_push_public_key,
        "configured": settings.push_enabled,
        "phase": "Web Push delivery lands in Phase 2; subscriptions are stored now.",
        "subscriptions": [
            {
                "id": s.id,
                "endpoint": s.endpoint[:60] + "...",
                "active": s.active,
                "failure_count": s.failure_count,
                "created_at": s.created_at.isoformat(),
                "expired_at": s.expired_at.isoformat() if s.expired_at else None,
            }
            for s in subs
        ],
    }


@router.post("/push/subscribe")
def subscribe_push(
    body: PushSubscriptionIn, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict:
    existing = (
        db.execute(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
        .scalars()
        .first()
    )
    if existing:
        existing.user_id = user.id
        existing.p256dh = body.keys["p256dh"]
        existing.auth = body.keys["auth"]
        existing.active = True
        existing.expired_at = None
        existing.failure_count = 0
    else:
        db.add(
            PushSubscription(
                user_id=user.id,
                endpoint=body.endpoint,
                p256dh=body.keys["p256dh"],
                auth=body.keys["auth"],
                user_agent=body.user_agent,
            )
        )
    db.commit()
    return {"subscribed": True}


@router.post("/push/unsubscribe")
def unsubscribe_push(
    body: dict, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict:
    endpoint = (body or {}).get("endpoint")
    if not endpoint:
        raise HTTPException(status_code=422, detail="endpoint is required")
    row = (
        db.execute(
            select(PushSubscription).where(
                PushSubscription.endpoint == endpoint, PushSubscription.user_id == user.id
            )
        )
        .scalars()
        .first()
    )
    if row:
        db.delete(row)
        db.commit()
    return {"unsubscribed": True}


@router.get("/digest")
def digest(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    hours: int = Query(24, ge=1, le=168),
) -> dict:
    return notif.build_digest(db, user.id, hours=hours)


@router.get("/me")
def me(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    since = utcnow() - dt.timedelta(hours=24)
    unread = int(
        db.execute(
            select(func.count(Notification.id)).where(
                Notification.user_id == user.id,
                Notification.read_at.is_(None),
                Notification.archived_at.is_(None),
            )
        ).scalar()
        or 0
    )
    return {
        "id": user.id,
        "display_name": user.display_name,
        "timezone": user.timezone,
        "unread_notifications": unread,
        "alerts_24h": int(
            db.execute(
                select(func.count(Notification.id)).where(
                    Notification.user_id == user.id, Notification.created_at >= since
                )
            ).scalar()
            or 0
        ),
    }
