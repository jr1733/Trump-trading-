"""Digest generation and delivery.

A digest is a notification like any other: it goes into the notification centre
first, and email/push are additional channels that may or may not be configured.
That ordering matters — if email is down, the digest is still *there*.

Scheduling is per user and in the user's own timezone: a "07:00 daily digest"
means 07:00 where they are, not 07:00 UTC. The worker ticks every few minutes
and asks "is it this user's digest hour, and have they already had today's?",
which makes the job idempotent and robust to restarts. The idempotency key
carries the local date, so a worker restart at 07:05 cannot send a second copy.

Quiet hours do not suppress a digest — a digest scheduled for 07:00 is a thing
the user asked for at 07:00, not an interruption.
"""

from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import NotificationPreference, User, utcnow
from . import notifications as notif
from .email import EmailSender

log = logging.getLogger(__name__)


def local_now(timezone: str) -> dt.datetime:
    try:
        return utcnow().astimezone(ZoneInfo(timezone))
    except Exception:
        return utcnow()


def is_digest_due(prefs: NotificationPreference, *, moment: dt.datetime | None = None) -> bool:
    """True when it is the user's digest hour in their own timezone."""
    if not prefs.digest_enabled or not settings.digest_enabled:
        return False
    local = (moment or utcnow()).astimezone(_zone(prefs.timezone))
    return local.hour == prefs.digest_hour_local


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except Exception:
        return ZoneInfo("UTC")


def render_digest_text(digest: dict) -> str:
    """Plain text body. Neutral wording throughout; no trading language."""
    lines: list[str] = []
    window = digest.get("window_hours", 24)
    lines.append(f"Digest for the last {window} hours.")
    lines.append("")

    def section(title: str, rows: list[str]) -> None:
        lines.append(title)
        lines.extend(rows if rows else ["  (nothing)"])
        lines.append("")

    section(
        "Top bullish signals:",
        [
            f"  {row['ticker']}: {row['score']:+.2f} ({row['label']}, N={row['n']})"
            for row in digest.get("top_bullish", [])
        ],
    )
    section(
        "Top bearish signals:",
        [
            f"  {row['ticker']}: {row['score']:+.2f} ({row['label']}, N={row['n']})"
            for row in digest.get("top_bearish", [])
        ],
    )
    section(
        "Watchlist events:",
        [
            f"  [{row['source_timestamp'][:16]}] {row['title'][:90]}"
            for row in digest.get("watchlist_events", [])
        ],
    )
    section(
        "Source health:",
        [
            f"  {row['source']}: {row['status']}"
            + (f" ({row['consecutive_failures']} consecutive failures)"
               if row["consecutive_failures"] else "")
            for row in digest.get("source_health", [])
        ],
    )

    summary = digest.get("notification_summary", {})
    section(
        "Notifications:",
        [
            f"  {summary.get('total', 0)} total, {summary.get('unread', 0)} unread",
            *[f"  {kind}: {count}" for kind, count in (summary.get("by_type") or {}).items()],
        ],
    )

    failures = digest.get("unresolved_failures", [])
    section(
        "Unresolved delivery failures:",
        [f"  {row['channel']} {row['status']}: {(row['response'] or '')[:80]}" for row in failures],
    )

    lines.append(
        "Research and information tool. Statistical associations between past "
        "announcements and past price moves are not causation and not a forecast."
    )
    return "\n".join(lines)


def digest_summary_line(digest: dict) -> str:
    """One-line body for the notification centre entry."""
    bullish = digest.get("top_bullish", [])
    bearish = digest.get("top_bearish", [])
    watchlist = digest.get("watchlist_events", [])
    degraded = [
        row["source"]
        for row in digest.get("source_health", [])
        if row["status"] not in ("ONLINE", "MANUAL_ONLY", "NEEDS_KEY", "DISABLED")
    ]

    parts = [
        f"{len(bullish)} bullish and {len(bearish)} bearish signals",
        f"{len(watchlist)} watchlist events",
    ]
    if degraded:
        parts.append(f"sources degraded: {', '.join(degraded)}")
    failures = digest.get("unresolved_failures", [])
    if failures:
        parts.append(f"{len(failures)} unresolved delivery failures")
    return "; ".join(parts) + ". Review analysis in the app."


def send_digest(
    db: Session,
    user: User,
    *,
    hours: int = 24,
    cadence: str = "daily",
    sender: EmailSender | None = None,
    push_provider: notif.PushProvider | None = None,
    moment: dt.datetime | None = None,
) -> dict:
    """Build and deliver one digest. Idempotent per user per local period."""
    prefs = notif.get_preferences(db, user.id)
    digest = notif.build_digest(db, user.id, hours=hours)
    local = (moment or utcnow()).astimezone(_zone(prefs.timezone))
    window = (
        local.strftime("%Y-%m-%d") if cadence == "daily" else local.strftime("%Y-%m-%dT%H")
    )

    outcome = notif.create_notification(
        db,
        user_id=user.id,
        notification_type="digest",
        title=notif.NEUTRAL_TITLES["digest"] if cadence == "daily" else "Hourly digest",
        body=digest_summary_line(digest),
        condition=f"digest:{cadence}",
        window=window,
        payload=digest,
        # A digest is scheduled, not interruptive: the user asked for it at this
        # hour, so by default quiet hours do not apply. That default is now the
        # user's to change -- if their quiet hours mean "nothing at all", the
        # digest waits like everything else.
        respect_quiet_hours=not prefs.digest_ignores_quiet_hours,
        # The hourly cap is never applied: it exists to stop a burst of alerts,
        # and one scheduled summary is not a burst. A digest silently eaten by a
        # rate limit is the digest you most needed to see.
        respect_rate_limit=False,
    )
    db.commit()

    result = {
        "created": outcome.created,
        "reason": outcome.suppressed_reason,
        "cadence": cadence,
        "window": window,
        "email": None,
        "push": None,
    }
    if not outcome.created or outcome.notification is None:
        return result

    notification = outcome.notification
    channels = prefs.channels or ["in_app"]

    if "email" in channels:
        entry = (sender or EmailSender()).send(
            db,
            user_id=user.id,
            to_address=user.email,
            subject=f"{settings.app_name}: {cadence} digest",
            body=render_digest_text(digest),
            notification_id=notification.id,
        )
        result["email"] = entry.status
    if "web_push" in channels:
        deliveries = notif.deliver_push(db, notification, push_provider)
        result["push"] = [d.status for d in deliveries]
    db.commit()
    return result


def run_due_digests(
    db: Session,
    *,
    sender: EmailSender | None = None,
    push_provider: notif.PushProvider | None = None,
    moment: dt.datetime | None = None,
) -> dict:
    """Worker entry point. Sends to every user whose digest hour it is."""
    summary = {"checked": 0, "sent": 0, "skipped": 0, "results": []}
    for user in db.execute(select(User)).scalars():
        summary["checked"] += 1
        prefs = notif.get_preferences(db, user.id)

        cadences: list[tuple[str, int]] = []
        if is_digest_due(prefs, moment=moment):
            cadences.append(("daily", 24))
        if settings.hourly_digest_enabled and prefs.digest_enabled:
            cadences.append(("hourly", 1))

        for cadence, hours in cadences:
            result = send_digest(
                db,
                user,
                hours=hours,
                cadence=cadence,
                sender=sender,
                push_provider=push_provider,
                moment=moment,
            )
            summary["results"].append({"user_id": user.id, **result})
            if result["created"]:
                summary["sent"] += 1
            else:
                summary["skipped"] += 1
    db.commit()
    return summary
