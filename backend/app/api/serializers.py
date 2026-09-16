"""Model -> response serialisation."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Event, Notification, NotificationDelivery, Signal
from ..schemas import AnalysisOut, EventOut, NotificationOut, SignalOut, TickerLink

EXCERPT_CHARS = 260


def excerpt(event: Event) -> str:
    text = (event.text or "").strip()
    return text if len(text) <= EXCERPT_CHARS else text[: EXCERPT_CHARS - 3] + "..."


def signal_out(signal: Signal) -> SignalOut:
    return SignalOut(
        id=signal.id,
        ticker=signal.ticker,
        horizon=signal.horizon,
        score=signal.score,
        label=signal.label,
        confidence=signal.confidence,
        sample_size=signal.sample_size,
        sample_flag=signal.sample_flag,
        components=signal.components or {},
        weights=signal.weights or {},
        uncertainties=(signal.uncertainties or {}).get("items", []),
        historical_stats=signal.historical_stats or {},
        created_at=signal.created_at,
    )


def analysis_out(event: Event) -> AnalysisOut | None:
    analysis = event.analysis
    if analysis is None:
        return None
    parsed = analysis.parsed or {}
    return AnalysisOut(
        status=analysis.status,
        model=analysis.model,
        event_type=analysis.event_type,
        sentiment=analysis.sentiment,
        market_impact=analysis.market_impact,
        confidence=analysis.confidence,
        time_horizon=analysis.time_horizon,
        reasoning=parsed.get("reasoning"),
        facts=parsed.get("facts") or [],
        stated_positions=parsed.get("stated_positions") or [],
        third_party_claims=parsed.get("third_party_claims") or [],
        speculation=parsed.get("speculation") or [],
        uncertainty=parsed.get("uncertainty") or [],
        validation_error=analysis.validation_error,
    )


def event_out(
    db: Session,
    event: Event,
    *,
    signals: list[Signal] | None = None,
    alerted_ids: set[str] | None = None,
) -> EventOut:
    if signals is None:
        signals = list(
            db.execute(select(Signal).where(Signal.event_id == event.id)).scalars()
        )
    return EventOut(
        id=event.id,
        source_key=event.source_key,
        author=event.author,
        title=event.title,
        text=event.text,
        excerpt=excerpt(event),
        url=event.url,
        event_type=event.event_type,
        source_timestamp=event.source_timestamp,
        ingestion_timestamp=event.ingestion_timestamp,
        processing_timestamp=event.processing_timestamp,
        relevance_score=event.relevance_score,
        relevant=event.relevant,
        analysis_status=event.analysis_status,
        is_historical=event.is_historical,
        tickers=[
            TickerLink(
                ticker=link.ticker,
                confidence=link.confidence,
                matched_alias=link.matched_alias,
                source=link.source,
                user_corrected=link.user_corrected,
            )
            for link in event.tickers
        ],
        analysis=analysis_out(event),
        signals=[signal_out(s) for s in signals],
        alert_triggered=event.id in (alerted_ids or set()),
    )


def notification_out(db: Session, note: Notification) -> NotificationOut:
    deliveries = list(
        db.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.notification_id == note.id
            )
        ).scalars()
    )
    return NotificationOut(
        id=note.id,
        notification_type=note.notification_type,
        severity=note.severity,
        title=note.title,
        body=note.body,
        event_id=note.event_id,
        ticker=note.ticker,
        created_at=note.created_at,
        read_at=note.read_at,
        archived_at=note.archived_at,
        payload=note.payload or {},
        deliveries=[
            {
                "channel": d.channel,
                "status": d.status,
                "retry_count": d.retry_count,
                "provider_response": d.provider_response,
                "created_at": d.created_at.isoformat(),
                "sent_at": d.sent_at.isoformat() if d.sent_at else None,
                "failed_at": d.failed_at.isoformat() if d.failed_at else None,
            }
            for d in deliveries
        ],
    )
