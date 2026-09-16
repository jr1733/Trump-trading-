"""Seeding and the historical-archive importer.

`seed_reference_data` is idempotent and safe to run on every start: it only
fills in rows that are missing, and never overwrites a user's edits (notably
`user_corrected` alias rows).
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    AlertRule,
    Entity,
    EntityAlias,
    NotificationPreference,
    Ticker,
    User,
    Watchlist,
    WatchlistTicker,
)
from ..sources.base import RawItem
from ..sources.registry import store_raw_items, sync_source_rows

log = logging.getLogger(__name__)

SEED_DIR = Path(__file__).resolve().parent


def _load(name: str) -> list[dict]:
    path = SEED_DIR / name
    return json.loads(path.read_text()) if path.exists() else []


def seed_reference_data(db: Session) -> dict[str, int]:
    """Tickers, entities, aliases, the default user, watchlist and alert rules."""
    counts = {"tickers": 0, "aliases": 0, "entities": 0}

    for row in _load("tickers.json"):
        if db.get(Ticker, row["symbol"]) is None:
            db.add(Ticker(**row))
            counts["tickers"] += 1
    db.flush()

    entities: dict[str, Entity] = {}
    for row in _load("aliases.json"):
        name = row.get("entity") or row["alias"]
        entity = entities.get(name)
        if entity is None:
            entity = db.execute(select(Entity).where(Entity.name == name)).scalars().first()
            if entity is None:
                entity = Entity(name=name)
                db.add(entity)
                db.flush()
                counts["entities"] += 1
            entities[name] = entity

        existing = (
            db.execute(
                select(EntityAlias).where(
                    EntityAlias.alias == row["alias"], EntityAlias.ticker == row["ticker"]
                )
            )
            .scalars()
            .first()
        )
        if existing is None:
            db.add(
                EntityAlias(
                    entity_id=entity.id,
                    alias=row["alias"],
                    ticker=row["ticker"],
                    confidence=row.get("confidence", "MEDIUM"),
                    ambiguous=bool(row.get("ambiguous", False)),
                )
            )
            counts["aliases"] += 1
    db.flush()

    sync_source_rows(db)
    db.commit()
    return counts


def seed_user(db: Session) -> User:
    """The single Phase 1 user, with a default watchlist and two alert rules."""
    user = db.execute(select(User).order_by(User.created_at)).scalars().first()
    if user is None:
        user = User(display_name="Operator", timezone="America/New_York")
        db.add(user)
        db.flush()

    prefs = (
        db.execute(select(NotificationPreference).where(NotificationPreference.user_id == user.id))
        .scalars()
        .first()
    )
    if prefs is None:
        db.add(NotificationPreference(user_id=user.id, timezone=user.timezone))

    watchlist = (
        db.execute(select(Watchlist).where(Watchlist.user_id == user.id)).scalars().first()
    )
    if watchlist is None:
        watchlist = Watchlist(user_id=user.id, name="Default")
        db.add(watchlist)
        db.flush()
        for symbol in ["AAPL", "NVDA", "TSLA", "XOM", "LMT"]:
            db.add(WatchlistTicker(watchlist_id=watchlist.id, ticker=symbol))

    existing_rules = db.execute(select(AlertRule).where(AlertRule.user_id == user.id)).scalars().all()
    if not existing_rules:
        db.add(
            AlertRule(
                user_id=user.id,
                name="Watchlist events",
                rule_type="new_event",
                tickers=["AAPL", "NVDA", "TSLA", "XOM", "LMT"],
                channels=["in_app", "web_push"],
            )
        )
        db.add(
            AlertRule(
                user_id=user.id,
                name="Signal threshold on watchlist",
                rule_type="signal_threshold",
                tickers=[],
                bullish_threshold=0.2,
                bearish_threshold=-0.2,
                # Default to not alerting on statistics we have already labelled
                # unreliable. Lower it deliberately if you want the noise.
                min_confidence=0.3,
                min_sample_size=10,
                channels=["in_app", "web_push"],
            )
        )
    db.commit()
    return user


# --------------------------------------------------------------------------
# Archive import
# --------------------------------------------------------------------------
def rows_to_items(rows: list[dict[str, Any]], *, source_key: str = "archive") -> list[RawItem]:
    """Normalise archive rows (CSV or JSON) into RawItems.

    Accepts the loose shapes people actually have: `id`/`post_id`,
    `created_at`/`timestamp`/`source_timestamp`, `text`/`content`.
    """
    import datetime as dt

    items: list[RawItem] = []
    for index, row in enumerate(rows):
        raw_ts = row.get("created_at") or row.get("timestamp") or row.get("source_timestamp")
        text = row.get("text") or row.get("content") or ""
        if not raw_ts or not text:
            log.warning("skipping archive row %d: missing timestamp or text", index)
            continue
        ts = dt.datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        payload = dict(row)
        payload.setdefault("historical", True)
        payload["text"] = text
        payload["title"] = row.get("title")
        payload["author"] = row.get("author")
        payload["url"] = row.get("url")
        items.append(
            RawItem(
                source_key=row.get("source") or source_key,
                external_id=str(row.get("id") or row.get("post_id") or f"{source_key}:{index}"),
                title=row.get("title"),
                text=text,
                author=row.get("author"),
                url=row.get("url"),
                media_url=row.get("media_url"),
                source_timestamp=ts,
                payload=payload,
            )
        )
    return items


def load_archive_file(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    return json.loads(path.read_text())


def import_archive(db: Session, rows: list[dict[str, Any]], *, source_key: str = "archive") -> int:
    """Insert archive rows as raw events. Returns the number of *new* rows.

    Re-importing the same archive inserts nothing -- dedup is on
    ``(source_key, external_id)``.
    """
    items = rows_to_items(rows, source_key=source_key)
    inserted = store_raw_items(db, items)
    db.commit()
    return inserted


def seed_sample_archive(db: Session) -> int:
    return import_archive(db, _load("archive_events.json"), source_key="archive")
