"""Canned analyses -- the "no API key" path for demos and tests.

This is **not** a model. It produces a deterministic, schema-valid analysis from
the rule-based scorers so the whole pipeline, the signal maths and the UI can be
exercised with no key and no network. Everything it writes is stamped with the
model name ``canned-mock``, which the UI displays verbatim, so a canned reading
is never mistaken for a real one.

Enable with ``LLM_FAKE_MODE=true``.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..pipeline.relevance import rule_based_sentiment, score_relevance
from .client import AnthropicClient, LLMOutcome
from .schema import AnalysisResult

CANNED_PATH = Path(__file__).resolve().parent.parent / "seed" / "canned_analyses.json"

FAKE_MODEL = "canned-mock"


def _load_canned() -> list[dict]:
    if not CANNED_PATH.exists():
        return []
    return json.loads(CANNED_PATH.read_text())


def _keyword_event_type(blob: str) -> str:
    table = [
        ("tariff", "tariff"),
        ("sanction", "sanction"),
        ("export control", "sanction"),
        ("trade agreement", "trade_deal"),
        ("trade deal", "trade_deal"),
        ("antitrust", "regulation"),
        ("regulation", "regulation"),
        ("oversight", "regulation"),
        ("permitting", "deregulation"),
        ("deregulat", "deregulation"),
        ("defense contract", "defense"),
        ("missile", "defense"),
        ("drilling", "energy"),
        ("pipeline", "energy"),
        ("lng", "energy"),
        ("tax", "fiscal_policy"),
        ("interest rate", "monetary_commentary"),
        ("federal reserve", "monetary_commentary"),
        ("nomination", "personnel"),
        ("immigration", "immigration"),
        ("lawsuit", "legal"),
    ]
    for needle, event_type in table:
        if needle in blob:
            return event_type
    return "company_mention"


class CannedAnthropicClient(AnthropicClient):
    """Drop-in replacement for `AnthropicClient` that never calls the network."""

    def __init__(self) -> None:  # noqa: D107 - deliberately skips the real client
        self._client = None
        self._supports_output_config = False
        self._canned = _load_canned()

    @property
    def available(self) -> bool:
        return True

    def _canned_for(self, title: str | None, text: str) -> dict | None:
        blob = f"{title or ''} {text}".lower()
        for row in self._canned:
            if all(term.lower() in blob for term in row.get("match_all", [])):
                return row["analysis"]
        return None

    def triage(self, *, source: str, title: str | None, text: str) -> LLMOutcome:
        verdict = score_relevance(text, title)
        return LLMOutcome(
            status="COMPLETE",
            parsed={
                "relevant": verdict.relevant,
                "score": verdict.score,
                "reason": f"canned triage: {verdict.reason}",
            },
            raw_response=json.dumps(
                {"relevant": verdict.relevant, "score": verdict.score, "reason": verdict.reason}
            ),
            model=FAKE_MODEL,
            attempts=1,
        )

    def analyse(
        self, *, source: str, author: str | None, timestamp: str, title: str | None, text: str
    ) -> LLMOutcome:
        blob = f"{title or ''} {text}".lower()
        canned = self._canned_for(title, text)
        if canned is None:
            sentiment = rule_based_sentiment(text, title)
            verdict = score_relevance(text, title)
            canned = {
                "event_type": _keyword_event_type(blob),
                "entities": [],
                "tickers": [],
                "sentiment": sentiment,
                "market_impact": round(sentiment * 0.7, 3),
                "confidence": round(min(0.75, 0.3 + verdict.score * 0.4), 3),
                "time_horizon": "days",
                "reasoning": (
                    "Canned analysis generated from the rule-based scorers. No model "
                    "was called."
                ),
                "facts": [],
                "stated_positions": [],
                "third_party_claims": [],
                "speculation": [],
                "uncertainty": [
                    "This analysis was produced by the canned offline scorer, not by a "
                    "language model. Treat it as placeholder structure, not as a reading "
                    "of the text."
                ],
            }

        parsed = AnalysisResult.model_validate(canned).model_dump()
        return LLMOutcome(
            status="COMPLETE",
            parsed=parsed,
            raw_response=json.dumps(parsed),
            model=FAKE_MODEL,
            attempts=1,
        )


def build_client() -> AnthropicClient:
    """Factory used by the pipeline: canned when configured, real otherwise."""
    from ..config import settings

    if settings.llm_fake_mode:
        return CannedAnthropicClient()
    return AnthropicClient()
