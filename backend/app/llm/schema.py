"""The analysis contract.

One definition drives three things: the JSON schema sent to the model, the
validation applied to what comes back, and the columns we persist. Keeping them
in one place is what makes "retry once with the validation error" meaningful.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

EVENT_TYPES = [
    "tariff",
    "sanction",
    "trade_deal",
    "regulation",
    "deregulation",
    "company_mention",
    "sector_comment",
    "personnel",
    "fiscal_policy",
    "monetary_commentary",
    "immigration",
    "energy",
    "defense",
    "legal",
    "other",
]

TIME_HORIZONS = ["minutes", "hours", "days", "weeks"]


class AnalysisResult(BaseModel):
    """Exactly the schema from the spec, with ranges enforced."""

    model_config = {"extra": "ignore"}

    event_type: str = "other"
    entities: list[str] = Field(default_factory=list)
    tickers: list[str] = Field(default_factory=list)
    sentiment: float = Field(ge=-1.0, le=1.0)
    market_impact: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    time_horizon: Literal["minutes", "hours", "days", "weeks"] = "days"
    reasoning: str = ""
    facts: list[str] = Field(default_factory=list)
    stated_positions: list[str] = Field(default_factory=list)
    third_party_claims: list[str] = Field(default_factory=list)
    speculation: list[str] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)

    @field_validator("event_type")
    @classmethod
    def _known_event_type(cls, value: str) -> str:
        cleaned = (value or "other").strip().lower().replace(" ", "_")
        return cleaned if cleaned in EVENT_TYPES else "other"

    @field_validator("tickers")
    @classmethod
    def _normalise_tickers(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for symbol in value:
            cleaned = (symbol or "").strip().upper().lstrip("$")
            if cleaned and cleaned not in out and len(cleaned) <= 16:
                out.append(cleaned)
        return out


JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "event_type": {"type": "string", "enum": EVENT_TYPES},
        "entities": {"type": "array", "items": {"type": "string"}},
        "tickers": {"type": "array", "items": {"type": "string"}},
        "sentiment": {"type": "number", "minimum": -1.0, "maximum": 1.0},
        "market_impact": {"type": "number", "minimum": -1.0, "maximum": 1.0},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "time_horizon": {"type": "string", "enum": TIME_HORIZONS},
        "reasoning": {"type": "string"},
        "facts": {"type": "array", "items": {"type": "string"}},
        "stated_positions": {"type": "array", "items": {"type": "string"}},
        "third_party_claims": {"type": "array", "items": {"type": "string"}},
        "speculation": {"type": "array", "items": {"type": "string"}},
        "uncertainty": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "event_type",
        "entities",
        "tickers",
        "sentiment",
        "market_impact",
        "confidence",
        "time_horizon",
        "reasoning",
        "facts",
        "stated_positions",
        "third_party_claims",
        "speculation",
        "uncertainty",
    ],
    "additionalProperties": False,
}


TRIAGE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reason": {"type": "string"},
    },
    "required": ["relevant", "score", "reason"],
    "additionalProperties": False,
}


class TriageResult(BaseModel):
    model_config = {"extra": "ignore"}

    relevant: bool
    score: float = Field(ge=0.0, le=1.0)
    reason: str = ""


__all__ = [
    "AnalysisResult",
    "TriageResult",
    "JSON_SCHEMA",
    "TRIAGE_SCHEMA",
    "EVENT_TYPES",
    "TIME_HORIZONS",
    "ValidationError",
]
