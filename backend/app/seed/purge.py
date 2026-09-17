"""Remove synthetic data from a database that is about to hold real data.

Local development seeds a mock source, a 286-event synthetic archive and a
deterministic set of fake price bars. All three look exactly like the real
thing -- same tables, same columns, same confident little percentages in the UI
-- so leaving any of them behind on a production box does not break anything
loudly. It quietly poisons every statistic that gets computed afterwards: a
comparable-event sample drawn half from invented events, a beta estimated
against invented prices.

`manage.py purge-mock` deletes them. `--dry-run` counts them first.

What counts as synthetic:

* events whose `source_key` is in SYNTHETIC_SOURCES, and everything hanging off
  them (analyses, shadow analyses, signals, historical matches, embeddings,
  ticker links, notifications, and the raw rows they came from);
* market prices whose `provider` is "mock".

What is deliberately NOT touched: tickers, aliases, entities, the user, the
watchlist, alert rules, notification preferences and source rows. Those are
reference data and configuration -- they are what `manage.py seed` loads, they
are not synthetic, and a production box needs them.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import (
    ClaudeAnalysis,
    Event,
    EventEmbedding,
    EventTicker,
    HistoricalEventMatch,
    MarketPrice,
    Notification,
    RawEvent,
    ShadowAnalysis,
    Signal,
)

#: Sources whose events are invented. "mock" is the local dev feed; "archive" is
#: the synthetic sample archive that `manage.py demo` imports.
SYNTHETIC_SOURCES = ("mock", "archive")

#: Market-data providers whose bars are invented.
SYNTHETIC_PRICE_PROVIDERS = ("mock",)


def count_synthetic(db: Session) -> dict[str, int]:
    """What `purge_synthetic` would delete, without deleting it."""
    event_ids = select(Event.id).where(Event.source_key.in_(SYNTHETIC_SOURCES))

    def count(model, *where) -> int:
        return int(db.execute(select(func.count()).select_from(model).where(*where)).scalar() or 0)

    return {
        "events": count(Event, Event.source_key.in_(SYNTHETIC_SOURCES)),
        "raw_events": count(RawEvent, RawEvent.source_key.in_(SYNTHETIC_SOURCES)),
        "analyses": count(ClaudeAnalysis, ClaudeAnalysis.event_id.in_(event_ids)),
        "shadow_analyses": count(ShadowAnalysis, ShadowAnalysis.event_id.in_(event_ids)),
        "signals": count(Signal, Signal.event_id.in_(event_ids)),
        "historical_matches": count(
            HistoricalEventMatch,
            HistoricalEventMatch.event_id.in_(event_ids)
            | HistoricalEventMatch.matched_event_id.in_(event_ids),
        ),
        "embeddings": count(EventEmbedding, EventEmbedding.event_id.in_(event_ids)),
        "event_tickers": count(EventTicker, EventTicker.event_id.in_(event_ids)),
        "notifications": count(Notification, Notification.event_id.in_(event_ids)),
        "market_prices": count(MarketPrice, MarketPrice.provider.in_(SYNTHETIC_PRICE_PROVIDERS)),
    }


def purge_synthetic(db: Session, *, all_notifications: bool = False) -> dict[str, int]:
    """Delete every synthetic row. Returns what was removed.

    Deletion order matters even though most foreign keys cascade:
    `notifications.event_id` is ON DELETE SET NULL, not CASCADE, so notifications
    about a mock event would otherwise survive as orphans with a null event and
    a body referring to something that no longer exists.
    """
    removed = count_synthetic(db)
    event_ids = select(Event.id).where(Event.source_key.in_(SYNTHETIC_SOURCES))

    # Notifications first -- SET NULL, so they must go before the events do.
    if all_notifications:
        # Digests and system alerts have no event_id, so they survive the
        # event-scoped delete below even though a digest generated during local
        # development summarises nothing but synthetic signals.
        removed["notifications"] = int(
            db.execute(select(func.count()).select_from(Notification)).scalar() or 0
        )
        db.execute(delete(Notification))
    else:
        db.execute(delete(Notification).where(Notification.event_id.in_(event_ids)))

    # Historical matches reference events twice; the cascade covers both sides,
    # but being explicit keeps this readable and order-independent.
    db.execute(
        delete(HistoricalEventMatch).where(
            HistoricalEventMatch.event_id.in_(event_ids)
            | HistoricalEventMatch.matched_event_id.in_(event_ids)
        )
    )
    db.execute(delete(Signal).where(Signal.event_id.in_(event_ids)))
    db.execute(delete(ShadowAnalysis).where(ShadowAnalysis.event_id.in_(event_ids)))
    db.execute(delete(ClaudeAnalysis).where(ClaudeAnalysis.event_id.in_(event_ids)))
    db.execute(delete(EventEmbedding).where(EventEmbedding.event_id.in_(event_ids)))
    db.execute(delete(EventTicker).where(EventTicker.event_id.in_(event_ids)))

    db.execute(delete(Event).where(Event.source_key.in_(SYNTHETIC_SOURCES)))
    db.execute(delete(RawEvent).where(RawEvent.source_key.in_(SYNTHETIC_SOURCES)))
    db.execute(
        delete(MarketPrice).where(MarketPrice.provider.in_(SYNTHETIC_PRICE_PROVIDERS))
    )

    db.commit()
    return removed
