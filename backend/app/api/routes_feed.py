"""Live feed, ticker pages, search and dashboard."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, String, cast, desc, func, or_, select
from sqlalchemy.orm import Session

from ..auth import current_user
from ..config import settings
from ..db import get_db
from ..market.service import MarketDataService
from ..models import (
    Event,
    EventTicker,
    Notification,
    Signal,
    SourceHealth,
    Ticker,
    User,
    utcnow,
)
from ..pipeline import historical
from ..schemas import TickerCorrectionIn
from .serializers import event_out, signal_out

router = APIRouter(prefix="/api", tags=["feed"])

SORTS = {
    "newest": lambda: desc(Event.source_timestamp),
    "oldest": lambda: Event.source_timestamp,
    "source": lambda: Event.source_key,
    "event_type": lambda: Event.event_type,
}


def _alerted_event_ids(db: Session, user: User, event_ids: list[str]) -> set[str]:
    if not event_ids:
        return set()
    rows = db.execute(
        select(Notification.event_id).where(
            Notification.user_id == user.id, Notification.event_id.in_(event_ids)
        )
    ).scalars()
    return {r for r in rows if r}


@router.get("/events")
def list_events(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort: str = Query("newest"),
    ticker: str | None = None,
    source: str | None = None,
    event_type: str | None = None,
    include_filtered: bool = False,
    include_historical: bool = False,
) -> dict:
    stmt: Select = select(Event)
    if not include_historical:
        stmt = stmt.where(Event.is_historical.is_(False))
    if not include_filtered:
        stmt = stmt.where(Event.relevant.is_(True))
    if source:
        stmt = stmt.where(Event.source_key == source)
    if event_type:
        stmt = stmt.where(Event.event_type == event_type)
    if ticker:
        stmt = stmt.join(EventTicker, EventTicker.event_id == Event.id).where(
            EventTicker.ticker == ticker.upper()
        )

    total = db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar()

    if sort == "signal":
        # Order by the strongest absolute signal attached to each event.
        strength = (
            select(Signal.event_id, func.max(func.abs(Signal.score)).label("strength"))
            .group_by(Signal.event_id)
            .subquery()
        )
        stmt = stmt.join(strength, strength.c.event_id == Event.id, isouter=True).order_by(
            desc(func.coalesce(strength.c.strength, 0.0))
        )
    elif sort == "ticker":
        stmt = stmt.join(
            EventTicker, EventTicker.event_id == Event.id, isouter=True
        ).order_by(EventTicker.ticker)
    else:
        stmt = stmt.order_by(SORTS.get(sort, SORTS["newest"])())

    events = list(db.execute(stmt.offset(offset).limit(limit)).scalars().unique())
    alerted = _alerted_event_ids(db, user, [e.id for e in events])
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [event_out(db, e, alerted_ids=alerted) for e in events],
    }


@router.get("/events/{event_id}")
def get_event(
    event_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    alerted = _alerted_event_ids(db, user, [event.id])
    return event_out(db, event, alerted_ids=alerted).model_dump()


@router.post("/events/{event_id}/tickers")
def correct_ticker(
    event_id: str,
    body: TickerCorrectionIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Manual ticker correction. Corrections are stored and reused.

    `remember_alias` writes the correction back into `entity_aliases` marked
    `user_corrected`, so the same text resolves the same way next time and the
    rules engine never downgrades it again.
    """
    from ..models import EntityAlias

    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")

    link = (
        db.execute(
            select(EventTicker).where(
                EventTicker.event_id == event_id, EventTicker.ticker == body.ticker
            )
        )
        .scalars()
        .first()
    )
    if body.remove:
        if link:
            db.delete(link)
    elif link is None:
        db.add(
            EventTicker(
                event_id=event_id,
                ticker=body.ticker,
                confidence=body.confidence,
                matched_alias="user correction",
                source="user",
                user_corrected=True,
            )
        )
    else:
        link.confidence = body.confidence
        link.source = "user"
        link.user_corrected = True

    if body.remember_alias and not body.remove:
        existing = (
            db.execute(
                select(EntityAlias).where(
                    EntityAlias.alias == body.remember_alias,
                    EntityAlias.ticker == body.ticker,
                )
            )
            .scalars()
            .first()
        )
        if existing is None:
            db.add(
                EntityAlias(
                    alias=body.remember_alias,
                    ticker=body.ticker,
                    confidence=body.confidence,
                    user_corrected=True,
                )
            )
        else:
            existing.confidence = body.confidence
            existing.user_corrected = True

    db.commit()
    db.refresh(event)
    return event_out(db, event).model_dump()


@router.get("/tickers")
def list_tickers(db: Session = Depends(get_db), _: User = Depends(current_user)) -> dict:
    rows = list(db.execute(select(Ticker).order_by(Ticker.symbol)).scalars())
    counts = dict(
        db.execute(
            select(EventTicker.ticker, func.count(EventTicker.id)).group_by(EventTicker.ticker)
        ).all()
    )
    return {
        "items": [
            {
                "symbol": t.symbol,
                "name": t.name,
                "asset_class": t.asset_class,
                "sector": t.sector,
                "sector_etf": t.sector_etf,
                "event_count": counts.get(t.symbol, 0),
            }
            for t in rows
        ]
    }


@router.get("/tickers/{symbol}")
def ticker_detail(
    symbol: str,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
    limit: int = Query(40, ge=1, le=200),
) -> dict:
    symbol = symbol.upper()
    meta = db.get(Ticker, symbol)
    market = MarketDataService(db)

    events = list(
        db.execute(
            select(Event)
            .join(EventTicker, EventTicker.event_id == Event.id)
            .where(EventTicker.ticker == symbol)
            .order_by(desc(Event.source_timestamp))
            .limit(limit)
        )
        .scalars()
        .unique()
    )

    latest_signal = (
        db.execute(
            select(Signal)
            .where(Signal.ticker == symbol)
            .order_by(desc(Signal.created_at))
            .limit(1)
        )
        .scalars()
        .first()
    )

    history = []
    for event in events:
        returns = market.compute_returns(symbol, event.source_timestamp)
        analysis = event.analysis
        history.append(
            {
                "event_id": event.id,
                "source_timestamp": event.source_timestamp.isoformat(),
                "title": event.title or event.text[:100],
                "source": event.source_key,
                "event_type": event.event_type,
                "is_historical": event.is_historical,
                "sentiment": analysis.sentiment if analysis else None,
                "returns": {h: r.as_dict() for h, r in returns.items()},
            }
        )

    stats = {}
    if events:
        subject = events[0]
        comparable = historical.build_comparable_set(
            db, market, event=subject, ticker=symbol, persist=False
        )
        stats = comparable.as_dict()
        db.rollback()  # persist=False, so discard anything the lookups touched

    return {
        "symbol": symbol,
        "name": meta.name if meta else symbol,
        "sector": meta.sector if meta else None,
        "sector_etf": meta.sector_etf if meta else None,
        "available_horizons": market.available_horizons(),
        "provider": market.provider.name,
        "supports_intraday": market.provider.supports_intraday(),
        "signal": signal_out(latest_signal).model_dump() if latest_signal else None,
        "event_history": history,
        "historical_stats": stats,
    }


@router.get("/tickers/{symbol}/chart")
def ticker_chart(
    symbol: str,
    event_id: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> dict:
    """Event-time series: -60m..+60m intraday if supported, else daily around the event."""
    symbol = symbol.upper()
    market = MarketDataService(db)
    anchor_event = db.get(Event, event_id) if event_id else None
    moment = anchor_event.source_timestamp if anchor_event else utcnow()

    from ..market import calendar as mcal

    anchor_ts, basis = mcal.anchor(moment)
    series: list[dict] = []
    resolution = "daily"

    if market.provider.supports_intraday():
        rows = market.ensure_intraday(
            symbol, anchor_ts - dt.timedelta(minutes=60), anchor_ts + dt.timedelta(minutes=60), "1m"
        )
        if rows:
            resolution = "1m"
            series = [
                {"ts": r.ts.isoformat(), "close": float(r.close), "volume": float(r.volume)}
                for r in rows
            ]

    if not series:
        day = anchor_ts.astimezone(mcal.EASTERN).date()
        rows = market.ensure_daily(
            symbol, day - dt.timedelta(days=30), day + dt.timedelta(days=10)
        )
        series = [
            {"ts": r.ts.isoformat(), "close": float(r.adjusted_close or r.close), "volume": float(r.volume)}
            for r in rows
        ]
    db.commit()

    return {
        "symbol": symbol,
        "resolution": resolution,
        "anchor_ts": anchor_ts.isoformat(),
        "basis": basis,
        "series": series,
    }


@router.get("/search")
def search_events(
    q: str = Query(min_length=2, max_length=200),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    limit: int = Query(50, ge=1, le=200),
    include_historical: bool = True,
) -> dict:
    """Full-text search over events, with each hit's subsequent returns."""
    market = MarketDataService(db)
    tsquery = func.websearch_to_tsquery("english", q)
    # Must match the expression behind ix_events_fts in migration 0001 exactly,
    # or Postgres will not use the index.
    document = func.to_tsvector(
        "english",
        func.coalesce(Event.title, "")
        + " "
        + func.coalesce(Event.text, "")
        + " "
        + func.coalesce(Event.source_key, ""),
    )
    stmt = (
        select(Event)
        .where(
            or_(
                document.op("@@")(tsquery),
                cast(Event.text, String).ilike(f"%{q}%"),
            )
        )
        .order_by(desc(Event.source_timestamp))
        .limit(limit)
    )
    if not include_historical:
        stmt = stmt.where(Event.is_historical.is_(False))

    events = list(db.execute(stmt).scalars().unique())
    alerted = _alerted_event_ids(db, user, [e.id for e in events])

    items = []
    for event in events:
        payload = event_out(db, event, alerted_ids=alerted).model_dump()
        primary = next(
            (t.ticker for t in event.tickers if t.confidence in ("HIGH", "MEDIUM")), None
        )
        payload["subsequent_returns"] = (
            {h: r.as_dict() for h, r in market.compute_returns(primary, event.source_timestamp).items()}
            if primary
            else {}
        )
        items.append(payload)
    db.commit()
    return {"query": q, "count": len(items), "items": items}


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    since = utcnow() - dt.timedelta(days=7)
    top = list(
        db.execute(
            select(Signal)
            .where(Signal.created_at >= since)
            .order_by(desc(func.abs(Signal.score)))
            .limit(8)
        ).scalars()
    )
    latest = list(
        db.execute(
            select(Event)
            .where(Event.relevant.is_(True), Event.is_historical.is_(False))
            .order_by(desc(Event.source_timestamp))
            .limit(6)
        )
        .scalars()
        .unique()
    )
    health = list(db.execute(select(SourceHealth)).scalars())

    market = MarketDataService(db)
    context = []
    for symbol in settings.market_context_symbols:
        today = dt.date.today()
        rows = market.ensure_daily(symbol, today - dt.timedelta(days=12), today)
        if len(rows) >= 2:
            last, prior = rows[-1], rows[-2]
            close = float(last.adjusted_close or last.close)
            prev = float(prior.adjusted_close or prior.close)
            context.append(
                {
                    "symbol": symbol,
                    "close": close,
                    "change_pct": round(((close / prev) - 1.0) * 100, 3) if prev else None,
                    "as_of": last.ts.isoformat(),
                }
            )
        else:
            context.append({"symbol": symbol, "close": None, "change_pct": None, "as_of": None})
    db.commit()

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
    alerts_today = int(
        db.execute(
            select(func.count(Notification.id)).where(
                Notification.user_id == user.id,
                Notification.created_at >= utcnow() - dt.timedelta(hours=24),
            )
        ).scalar()
        or 0
    )
    from ..models import NotificationDelivery

    failed = int(
        db.execute(
            select(func.count(NotificationDelivery.id)).where(
                NotificationDelivery.status.in_(["FAILED", "EXPIRED"]),
                NotificationDelivery.created_at >= utcnow() - dt.timedelta(hours=24),
            )
        ).scalar()
        or 0
    )

    return {
        "top_signals": [signal_out(s).model_dump() for s in top],
        "latest_events": [event_out(db, e).model_dump() for e in latest],
        "market_context": context,
        "source_health": [
            {
                "source": h.source_key,
                "status": h.status,
                "consecutive_failures": h.consecutive_failures,
                "items_last_poll": h.items_last_poll,
                "last_success_at": h.last_success_at.isoformat() if h.last_success_at else None,
                "last_error": h.last_error,
            }
            for h in health
        ],
        "notification_summary": {
            "unread": unread,
            "alerts_today": alerts_today,
            "failed_deliveries": failed,
            "push_status": "configured" if settings.push_enabled else "not configured (Phase 2)",
            "next_digest": "daily digest (Phase 3)",
        },
    }
