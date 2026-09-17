"""Pydantic request/response models.

Request bodies are validated here so no endpoint parses raw dicts.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class TickerLink(BaseModel):
    ticker: str
    confidence: str
    matched_alias: str | None = None
    source: str = "rules"
    user_corrected: bool = False


class AnalysisOut(BaseModel):
    status: str
    model: str | None = None
    event_type: str | None = None
    sentiment: float | None = None
    market_impact: float | None = None
    confidence: float | None = None
    time_horizon: str | None = None
    reasoning: str | None = None
    facts: list[str] = Field(default_factory=list)
    stated_positions: list[str] = Field(default_factory=list)
    third_party_claims: list[str] = Field(default_factory=list)
    speculation: list[str] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)
    validation_error: str | None = None


class SignalOut(BaseModel):
    id: str
    ticker: str
    horizon: str
    score: float
    label: str
    confidence: float
    sample_size: int
    sample_flag: str
    components: dict[str, Any] = Field(default_factory=dict)
    weights: dict[str, Any] = Field(default_factory=dict)
    uncertainties: list[str] = Field(default_factory=list)
    historical_stats: dict[str, Any] = Field(default_factory=dict)
    created_at: dt.datetime


class EventOut(BaseModel):
    id: str
    source_key: str
    author: str | None = None
    title: str | None = None
    text: str
    excerpt: str
    url: str | None = None
    event_type: str
    source_timestamp: dt.datetime
    ingestion_timestamp: dt.datetime
    processing_timestamp: dt.datetime | None = None
    relevance_score: float
    relevant: bool
    analysis_status: str
    is_historical: bool = False
    tickers: list[TickerLink] = Field(default_factory=list)
    analysis: AnalysisOut | None = None
    signals: list[SignalOut] = Field(default_factory=list)
    alert_triggered: bool = False


class WatchlistTickerIn(BaseModel):
    ticker: str
    notes: str | None = None

    @field_validator("ticker")
    @classmethod
    def _upper(cls, value: str) -> str:
        cleaned = value.strip().upper().lstrip("$")
        if not cleaned or len(cleaned) > 16 or not cleaned.replace(".", "").replace("-", "").isalnum():
            raise ValueError("invalid ticker symbol")
        return cleaned


class AlertRuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    rule_type: Literal["new_event", "signal_threshold", "similar_topic"] = "new_event"
    enabled: bool = True
    tickers: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    event_types: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    channels: list[Literal["in_app", "web_push", "email"]] = Field(
        default_factory=lambda: ["in_app"]
    )
    bullish_threshold: float | None = Field(default=None, ge=-1.0, le=1.0)
    bearish_threshold: float | None = Field(default=None, ge=-1.0, le=1.0)
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    min_sample_size: int = Field(default=0, ge=0, le=10000)
    include_low_confidence_tickers: bool = False
    repeat_alerts: bool = False
    max_per_hour: int = Field(default=20, ge=1, le=500)
    grouped_delivery: bool = False
    search_query: str | None = None

    @field_validator("tickers")
    @classmethod
    def _upper_all(cls, value: list[str]) -> list[str]:
        return [v.strip().upper().lstrip("$") for v in value if v.strip()]


class PreferencesIn(BaseModel):
    enabled: bool = True
    quiet_hours_start: int | None = Field(default=None, ge=0, le=23)
    quiet_hours_end: int | None = Field(default=None, ge=0, le=23)
    timezone: str = "America/New_York"
    channels: list[Literal["in_app", "web_push", "email"]] = Field(
        default_factory=lambda: ["in_app"]
    )
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    min_sample_size: int = Field(default=0, ge=0, le=10000)
    max_per_hour: int = Field(default=30, ge=1, le=1000)
    digest_enabled: bool = True
    digest_hour_local: int = Field(default=7, ge=0, le=23)
    #: A digest is scheduled rather than interruptive, so it is delivered inside
    #: quiet hours by default. False makes quiet hours absolute.
    digest_ignores_quiet_hours: bool = True

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value


class PushSubscriptionIn(BaseModel):
    endpoint: str = Field(min_length=8, max_length=2000)
    keys: dict[str, str]
    user_agent: str | None = None

    @field_validator("keys")
    @classmethod
    def _has_keys(cls, value: dict[str, str]) -> dict[str, str]:
        if "p256dh" not in value or "auth" not in value:
            raise ValueError("subscription keys must include p256dh and auth")
        return value


class TickerCorrectionIn(BaseModel):
    ticker: str
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = "HIGH"
    remove: bool = False
    remember_alias: str | None = None

    @field_validator("ticker")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper().lstrip("$")


class NotificationOut(BaseModel):
    id: str
    notification_type: str
    severity: str
    title: str
    body: str
    event_id: str | None = None
    ticker: str | None = None
    created_at: dt.datetime
    read_at: dt.datetime | None = None
    archived_at: dt.datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    deliveries: list[dict[str, Any]] = Field(default_factory=list)


class BacktestIn(BaseModel):
    """Backtest request.

    `sentiment_mode` defaults to `rule_based` deliberately: an LLM-scored
    backtest may be contaminated by the model's own knowledge of what followed
    these events, and the default should be the clean one.
    """

    start: dt.date
    end: dt.date
    ticker: str | None = None
    event_type: str | None = None
    min_signal: float = Field(default=0.2, ge=0.0, le=1.0)
    holding_days: int = Field(default=5, ge=1, le=60)
    sentiment_mode: Literal["rule_based", "llm"] = "rule_based"
    include_low_confidence: bool = False
    #: Keep only non-overlapping holding windows. **Defaults to true**: the
    #: overlapping number flatters every dispersion statistic, so the honest one
    #: should be what you see without asking. Turn it off to see the larger,
    #: more correlated sample.
    non_overlapping_only: bool = True
    #: Restrict to events after DEPLOYED_AT. Requires DEPLOYED_AT to be set.
    forward_only: bool = False
    train_fraction: float = Field(default=0.6, gt=0.0, lt=1.0)
    validation_fraction: float = Field(default=0.2, ge=0.0, lt=1.0)

    @field_validator("ticker")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.strip().upper().lstrip("$") if value else None

    @model_validator(mode="after")
    def _check_range(self) -> "BacktestIn":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if (self.end - self.start).days < 30:
            raise ValueError("a backtest window shorter than 30 days cannot be split")
        if self.train_fraction + self.validation_fraction >= 1.0:
            raise ValueError("train and validation fractions must leave room for a test split")
        return self
