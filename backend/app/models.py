"""SQLAlchemy models.

Conventions
-----------
* Every timestamp column is ``TIMESTAMP WITH TIME ZONE`` and always stores UTC.
  Conversion to the user's local time happens in the browser, never here.
* Every row that belongs to a person carries ``user_id``. Phase 1 is single-user
  (one seeded row), but nothing in the schema assumes that.
* Idempotency is enforced by unique constraints, not by application-level
  "check then insert" -- retries must be safe under concurrency.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    true as sa_true,
)
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import settings


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[float]: ARRAY(Float)}


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


TS = DateTime(timezone=True)


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, nullable=True)
    display_name: Mapped[str] = mapped_column(String(120), default="Operator")
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


# --------------------------------------------------------------------------
# Sources and their health
# --------------------------------------------------------------------------
class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(32))  # rss | api | mock | manual
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[str] = mapped_column(String(16), default="normal")  # high|normal|slow
    poll_interval_minutes: Mapped[int] = mapped_column(Integer, default=10)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class SourceHealth(Base):
    __tablename__ = "source_health"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="ONLINE")  # ONLINE|DEGRADED|ERROR
    last_success_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    items_last_poll: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------
# Raw and normalised events
# --------------------------------------------------------------------------
class RawEvent(Base):
    """Untouched payload exactly as the source returned it."""

    __tablename__ = "raw_events"
    __table_args__ = (
        UniqueConstraint("source_key", "external_id", name="uq_raw_events_source_external"),
        Index("ix_raw_events_ingested", "ingestion_timestamp"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_key: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(512))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    source_timestamp: Mapped[dt.datetime] = mapped_column(TS)
    ingestion_timestamp: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    process_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Event(Base):
    """Normalised, deduplicated event. This is the stable ID everything hangs off."""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_events_content_hash"),
        Index("ix_events_source_ts", "source_timestamp"),
        Index("ix_events_type", "event_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    raw_event_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("raw_events.id", ondelete="SET NULL"), nullable=True
    )
    source_key: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(512))
    author: Mapped[str | None] = mapped_column(String(160), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(48), default="other")

    source_timestamp: Mapped[dt.datetime] = mapped_column(TS)
    ingestion_timestamp: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    processing_timestamp: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)

    relevance_score: Mapped[float] = mapped_column(Float, default=0.0)
    relevance_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    relevant: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # PENDING | COMPLETE | FAILED | UNAVAILABLE | SKIPPED
    analysis_status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    analysis_attempts: Mapped[int] = mapped_column(Integer, default=0)
    pipeline_stage: Mapped[str] = mapped_column(String(32), default="stored")
    is_historical: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    tickers: Mapped[list["EventTicker"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", lazy="selectin"
    )
    analysis: Mapped["ClaudeAnalysis | None"] = relationship(
        back_populates="event", uselist=False, lazy="selectin"
    )


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), default="company")
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class EntityAlias(Base):
    """entity alias -> ticker, with a base confidence for that alias."""

    __tablename__ = "entity_aliases"
    __table_args__ = (
        UniqueConstraint("alias", "ticker", name="uq_entity_alias_ticker"),
        Index("ix_entity_aliases_alias", "alias"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    entity_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="CASCADE"), nullable=True
    )
    alias: Mapped[str] = mapped_column(String(200))
    ticker: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[str] = mapped_column(String(8), default="MEDIUM")  # HIGH|MEDIUM|LOW
    # Aliases that are ordinary English words ("apple", "delta") only ever match
    # with LOW confidence unless corroborated -- see pipeline/ticker_match.py.
    ambiguous: Mapped[bool] = mapped_column(Boolean, default=False)
    user_corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class Ticker(Base):
    __tablename__ = "tickers"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    asset_class: Mapped[str] = mapped_column(String(24), default="equity")
    sector: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sector_etf: Mapped[str | None] = mapped_column(String(16), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class EventTicker(Base):
    __tablename__ = "event_tickers"
    __table_args__ = (
        UniqueConstraint("event_id", "ticker", name="uq_event_ticker"),
        Index("ix_event_tickers_ticker", "ticker"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    ticker: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[str] = mapped_column(String(8), default="MEDIUM")
    matched_alias: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="rules")  # rules|claude|user
    user_corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)

    event: Mapped[Event] = relationship(back_populates="tickers")


# --------------------------------------------------------------------------
# LLM analysis
# --------------------------------------------------------------------------
class ClaudeAnalysis(Base):
    __tablename__ = "claude_analyses"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_claude_analyses_content_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    event_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="CASCADE"), nullable=True, index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="COMPLETE")
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    validation_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    event_type: Mapped[str | None] = mapped_column(String(48), nullable=True)
    sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_impact: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_horizon: Mapped[str | None] = mapped_column(String(16), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)

    event: Mapped[Event | None] = relationship(back_populates="analysis")


class EventEmbedding(Base):
    """One vector per event, stored in pgvector.

    `provider` and `model` travel with the vector so a similarity score can
    always be attributed, and so a provider change is detectable (rows whose
    provider no longer matches the configured one are re-embedded rather than
    silently compared against vectors from a different space).
    """

    __tablename__ = "event_embeddings"

    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(48), default="hashing")
    model: Mapped[str] = mapped_column(String(96))
    dim: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dim))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class LLMUsage(Base):
    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llm_usage_created", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    purpose: Mapped[str] = mapped_column(String(32))  # triage | analysis
    model: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------
class MarketPrice(Base):
    __tablename__ = "market_prices"
    __table_args__ = (
        UniqueConstraint("symbol", "interval", "ts", name="uq_market_prices_sym_int_ts"),
        Index("ix_market_prices_symbol_ts", "symbol", "ts"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    symbol: Mapped[str] = mapped_column(String(16))
    interval: Mapped[str] = mapped_column(String(8))  # 1d | 1m | 5m ...
    ts: Mapped[dt.datetime] = mapped_column(TS)
    open: Mapped[float] = mapped_column(Numeric(18, 6))
    high: Mapped[float] = mapped_column(Numeric(18, 6))
    low: Mapped[float] = mapped_column(Numeric(18, 6))
    close: Mapped[float] = mapped_column(Numeric(18, 6))
    adjusted_close: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    volume: Mapped[float] = mapped_column(Numeric(20, 2), default=0)
    provider: Mapped[str] = mapped_column(String(24), default="mock")
    fetched_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class HistoricalEventMatch(Base):
    """A past event judged comparable to a current event, for a given ticker."""

    __tablename__ = "historical_event_matches"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "ticker", "matched_event_id", name="uq_hist_match"
        ),
        Index("ix_hist_match_event", "event_id", "ticker"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="CASCADE")
    )
    matched_event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="CASCADE")
    )
    ticker: Mapped[str] = mapped_column(String(16))
    similarity: Mapped[float] = mapped_column(Float, default=0.0)
    match_basis: Mapped[str] = mapped_column(String(32), default="event_type+ticker")
    returns: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


# --------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------
class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("event_id", "ticker", name="uq_signal_event_ticker"),
        Index("ix_signals_ticker_created", "ticker", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    ticker: Mapped[str] = mapped_column(String(16))
    horizon: Mapped[str] = mapped_column(String(8), default="1d")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    label: Mapped[str] = mapped_column(String(24), default="NEUTRAL")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    sample_flag: Mapped[str] = mapped_column(String(16), default="unreliable")
    components: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    historical_stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    uncertainties: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    weights: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class SignalState(Base):
    """Armed/disarmed state for threshold crossing, per user+ticker+direction."""

    __tablename__ = "signal_state"
    __table_args__ = (
        UniqueConstraint("user_id", "ticker", "direction", name="uq_signal_state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    ticker: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str] = mapped_column(String(8))  # bullish | bearish
    armed: Mapped[bool] = mapped_column(Boolean, default=True)
    # Incremented each time the signal comes back inside the band and re-arms.
    # It is part of the notification idempotency key, so repeated evaluations
    # inside one armed cycle dedupe while a genuine later crossing does not.
    cycle: Mapped[int] = mapped_column(Integer, default=0)
    last_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_crossed_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------
# Watchlists and alert rules
# --------------------------------------------------------------------------
class Watchlist(Base):
    __tablename__ = "watchlists"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_watchlist_user_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120), default="Default")
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class WatchlistTicker(Base):
    __tablename__ = "watchlist_tickers"
    __table_args__ = (
        UniqueConstraint("watchlist_id", "ticker", name="uq_watchlist_ticker"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    watchlist_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("watchlists.id", ondelete="CASCADE"), index=True
    )
    ticker: Mapped[str] = mapped_column(String(16))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class AlertRule(Base):
    __tablename__ = "alert_rules"
    __table_args__ = (Index("ix_alert_rules_user", "user_id", "enabled"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(160))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # new_event | signal_threshold | similar_topic | system
    rule_type: Mapped[str] = mapped_column(String(32), default="new_event")
    tickers: Mapped[dict[str, Any]] = mapped_column(JSONB, default=list)
    keywords: Mapped[dict[str, Any]] = mapped_column(JSONB, default=list)
    entities: Mapped[dict[str, Any]] = mapped_column(JSONB, default=list)
    event_types: Mapped[dict[str, Any]] = mapped_column(JSONB, default=list)
    sources: Mapped[dict[str, Any]] = mapped_column(JSONB, default=list)
    channels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=lambda: ["in_app"])
    bullish_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    bearish_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    min_sample_size: Mapped[int] = mapped_column(Integer, default=0)
    include_low_confidence_tickers: Mapped[bool] = mapped_column(Boolean, default=False)
    repeat_alerts: Mapped[bool] = mapped_column(Boolean, default=False)
    max_per_hour: Mapped[int] = mapped_column(Integer, default=20)
    grouped_delivery: Mapped[bool] = mapped_column(Boolean, default=False)
    search_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"
    __table_args__ = (UniqueConstraint("user_id", name="uq_notif_pref_user"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    quiet_hours_start: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0-23 local
    quiet_hours_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    channels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=lambda: ["in_app"])
    min_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    min_sample_size: Mapped[int] = mapped_column(Integer, default=0)
    max_per_hour: Mapped[int] = mapped_column(Integer, default=30)
    digest_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    digest_hour_local: Mapped[int] = mapped_column(Integer, default=7)
    # A digest is scheduled, not interruptive, so by default it is delivered
    # even inside quiet hours. Set false if quiet hours mean nothing at all.
    digest_ignores_quiet_hours: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa_true()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------
class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_notifications_idempotency"),
        Index("ix_notifications_user_created", "user_id", "created_at"),
        Index("ix_notifications_unread", "user_id", "read_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    # new_event | signal_threshold | similar_topic | system | digest
    notification_type: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")  # info|warning|critical
    title: Mapped[str] = mapped_column(String(240))
    body: Mapped[str] = mapped_column(Text)
    event_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )
    ticker: Mapped[str | None] = mapped_column(String(16), nullable=True)
    rule_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("alert_rules.id", ondelete="SET NULL"), nullable=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    read_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    archived_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        Index("ix_deliveries_notification", "notification_id"),
        Index("ix_deliveries_status", "status"),
        Index("ix_deliveries_retry", "status", "next_retry_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    notification_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("notifications.id", ondelete="CASCADE")
    )
    channel: Mapped[str] = mapped_column(String(16))  # in_app | web_push | email
    # PENDING | SENT | FAILED | UNAVAILABLE | EXPIRED
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    provider_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    subscription_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    sent_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    read_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    failed_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    __table_args__ = (UniqueConstraint("endpoint", name="uq_push_endpoint"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    endpoint: Mapped[str] = mapped_column(Text)
    p256dh: Mapped[str] = mapped_column(Text)
    auth: Mapped[str] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    expired_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)


class EmailDeliveryLog(Base):
    __tablename__ = "email_delivery_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36))
    notification_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    to_address: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


# --------------------------------------------------------------------------
# Backtests and jobs
# --------------------------------------------------------------------------
class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    results: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    sentiment_mode: Mapped[str] = mapped_column(String(16), default="rule_based")
    llm_contaminated: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    completed_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (Index("ix_job_runs_name_started", "job_name", "started_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="RUNNING")
    started_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    items_processed: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = [n for n in dir() if n[0].isupper()]
