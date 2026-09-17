"""The cheapest configuration: Haiku for both triage and analysis.

Two things broke when the cost advice was actually followed, and both are
regression-tested here:

1. **`effort` errors on Haiku 4.5.** It was sent unconditionally, so switching
   ANTHROPIC_ANALYSIS_MODEL to Haiku while leaving ANTHROPIC_ANALYSIS_EFFORT at
   its default made every analysis call fail with a 400.
2. **Dated model IDs missed the pricing table.** `claude-haiku-4-5-20251001` is
   a valid ID, but the table is keyed on the undated form, so it fell through to
   the Opus default rate -- overstating a Haiku bill by 5x on exactly the page
   you would consult to decide whether Haiku was worth using.

The rest covers structured JSON on a small model, which is where reliability
actually differs: a smaller model is likelier to wrap JSON in prose or a
markdown fence, so the parser has to cope rather than assume.
"""

from __future__ import annotations

import json

import pytest

from app.llm import prompts
from app.llm.client import (
    MODEL_PRICING,
    AnthropicClient,
    _normalise_model,
    estimate_cost,
    extract_json,
    supports_effort,
)

HAIKU = "claude-haiku-4-5-20251001"

VALID = {
    "event_type": "tariff",
    "sentiment": -0.6,
    "market_impact": 0.7,
    "confidence": 0.75,
    "time_horizon": "days",
    "tickers": ["NVDA", "TSM"],
    "summary": "New tariffs proposed on imported semiconductors.",
    "facts": ["A tariff rate of 25% was named."],
    "positions": ["The administration supports domestic fabrication."],
    "claims": ["Prices will not rise for consumers."],
    "speculation": ["Other countries may retaliate."],
}


# --- model id normalisation ----------------------------------------------
@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("claude-haiku-4-5", "claude-haiku-4-5"),
        ("claude-opus-5", "claude-opus-5"),
        ("  claude-sonnet-5  ", "claude-sonnet-5"),
        ("", ""),
    ],
)
def test_dated_snapshot_ids_normalise(model, expected):
    assert _normalise_model(model) == expected


def test_a_dated_haiku_id_is_priced_as_haiku():
    """The 5x overstatement regression."""
    dated = estimate_cost(HAIKU, 1_000_000, 1_000_000)
    undated = estimate_cost("claude-haiku-4-5", 1_000_000, 1_000_000)

    assert dated == undated
    assert dated == pytest.approx(MODEL_PRICING["claude-haiku-4-5"][0]
                                  + MODEL_PRICING["claude-haiku-4-5"][1])
    opus = estimate_cost("claude-opus-5", 1_000_000, 1_000_000)
    assert dated < opus, "Haiku must not be priced at the Opus rate"


def test_an_unknown_model_falls_back_to_the_expensive_rate():
    """Erring high: an underestimate is the dangerous direction on a bill."""
    unknown = estimate_cost("claude-something-new", 1_000_000, 0)
    assert unknown >= estimate_cost("claude-opus-5", 1_000_000, 0)


# --- effort gating --------------------------------------------------------
@pytest.mark.parametrize(
    "model,expected",
    [
        (HAIKU, False),
        ("claude-haiku-4-5", False),
        ("claude-opus-5", True),
        ("claude-sonnet-5", True),
        ("claude-opus-4-8", True),
        ("claude-something-unreleased", False),  # unknown -> do not risk a 400
    ],
)
def test_effort_support_is_an_allowlist(model, expected):
    assert supports_effort(model) is expected


class RecordingMessages:
    """Captures the kwargs of every request, and can be told to reject some."""

    def __init__(self, responses, reject_effort=False, reject_output_config=False):
        self._responses = list(responses)
        self.reject_effort = reject_effort
        self.reject_output_config = reject_output_config
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        config = kwargs.get("output_config") or {}
        if self.reject_effort and "effort" in config:
            raise ValueError("output_config.effort: not supported for this model")
        if self.reject_output_config and config:
            raise ValueError("output_config is not supported")

        # Repeat the last body rather than falling back to a valid one: the
        # client retries once, so a fixture that heals on the retry would be
        # testing the retry instead of the failure path.
        body = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return type(
            "Response",
            (),
            {
                "content": [type("Block", (), {"type": "text", "text": body})()],
                "usage": type(
                    "Usage", (), {"input_tokens": 500, "output_tokens": 200,
                                  "cache_read_input_tokens": 0}
                )(),
            },
        )()


class FakeSDK:
    def __init__(self, messages):
        self.messages = messages


def client_for(messages) -> AnthropicClient:
    # `available` is a read-only property derived from the injected client.
    return AnthropicClient(client=FakeSDK(messages))


def test_effort_is_not_sent_to_haiku(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_analysis_model", HAIKU)
    monkeypatch.setattr(settings, "anthropic_analysis_effort", "medium")

    messages = RecordingMessages([json.dumps(VALID)])
    outcome = client_for(messages).analyse(
        source="mock", author="a", timestamp="2026-03-04T15:00:00+00:00",
        title="t", text="tariffs on chips",
    )

    assert outcome.status == "COMPLETE"
    config = messages.calls[0].get("output_config") or {}
    assert "effort" not in config, "Haiku rejects effort; it must not be sent"
    assert "format" in config, "the JSON schema must still be sent"


def test_effort_is_sent_to_a_model_that_takes_it(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_analysis_model", "claude-opus-5")
    monkeypatch.setattr(settings, "anthropic_analysis_effort", "medium")

    messages = RecordingMessages([json.dumps(VALID)])
    client_for(messages).analyse(
        source="mock", author="a", timestamp="2026-03-04T15:00:00+00:00",
        title="t", text="tariffs on chips",
    )
    assert (messages.calls[0].get("output_config") or {}).get("effort") == "medium"


def test_an_effort_rejection_keeps_the_json_schema(monkeypatch):
    """The belt-and-braces path, for a model the allowlist has not caught up
    with. Losing structured output to an effort complaint would trade the
    guarantee the parser depends on for a tuning knob."""
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_analysis_model", "claude-opus-5")
    monkeypatch.setattr(settings, "anthropic_analysis_effort", "medium")

    messages = RecordingMessages([json.dumps(VALID)], reject_effort=True)
    outcome = client_for(messages).analyse(
        source="mock", author="a", timestamp="2026-03-04T15:00:00+00:00",
        title="t", text="tariffs on chips",
    )

    assert outcome.status == "COMPLETE"
    assert len(messages.calls) == 2, "one rejected call, one retry"
    retry_config = messages.calls[1].get("output_config") or {}
    assert "effort" not in retry_config
    assert "format" in retry_config, "the schema must survive the retry"


# --- structured JSON on a small model ------------------------------------
def analyse_with(body: str):
    messages = RecordingMessages([body])
    return client_for(messages).analyse(
        source="mock", author="a", timestamp="2026-03-04T15:00:00+00:00",
        title="t", text="tariffs on chips",
    ), messages


def test_clean_json_parses():
    outcome, _ = analyse_with(json.dumps(VALID))
    assert outcome.status == "COMPLETE"
    assert outcome.parsed["sentiment"] == -0.6
    assert outcome.parsed["tickers"] == ["NVDA", "TSM"]


def test_a_markdown_fence_parses():
    """Smaller models wrap JSON in fences more often, schema or no schema."""
    outcome, _ = analyse_with(f"```json\n{json.dumps(VALID)}\n```")
    assert outcome.status == "COMPLETE"
    assert outcome.parsed["event_type"] == "tariff"


def test_leading_prose_parses():
    outcome, _ = analyse_with(
        "Here is the analysis you asked for:\n\n" + json.dumps(VALID)
    )
    assert outcome.status == "COMPLETE"


def test_trailing_prose_parses():
    outcome, _ = analyse_with(
        json.dumps(VALID) + "\n\nLet me know if you would like more detail."
    )
    assert outcome.status == "COMPLETE"


# --- malformed responses --------------------------------------------------
@pytest.mark.parametrize(
    "body,label",
    [
        ("", "empty response"),
        ("I cannot analyse this text.", "prose with no JSON at all"),
        ("{", "truncated JSON"),
        ('{"event_type": "tariff",}', "trailing comma"),
        ("[1, 2, 3]", "a JSON array rather than an object"),
        ("null", "JSON null"),
    ],
)
def test_malformed_responses_fail_cleanly(body, label):
    """A bad response must produce FAILED with the raw text kept for the
    data-quality page -- never a crash, and never a half-filled analysis."""
    outcome, _ = analyse_with(body)

    assert outcome.status == "FAILED", label
    assert outcome.parsed is None
    assert outcome.error, "the error is what shows up under malformed model output"


def test_a_response_missing_required_fields_is_rejected():
    outcome, _ = analyse_with(json.dumps({"event_type": "tariff"}))
    assert outcome.status == "FAILED"
    assert outcome.parsed is None


def test_out_of_range_values_are_rejected():
    """Sentiment outside [-1, 1] would sail straight into the signal."""
    body = dict(VALID, sentiment=7.5)
    outcome, _ = analyse_with(json.dumps(body))
    assert outcome.status == "FAILED"


def test_a_malformed_response_still_reports_tokens():
    """A failed call costs money; it must still appear on the cost page."""
    outcome, _ = analyse_with("not json")
    assert outcome.input_tokens > 0
    assert outcome.cost > 0


def test_extract_json_handles_the_shapes_directly():
    assert extract_json(json.dumps(VALID))["event_type"] == "tariff"
    assert extract_json(f"```json\n{json.dumps(VALID)}\n```")["event_type"] == "tariff"
    assert extract_json("```\n{\"a\": 1}\n```") == {"a": 1}
    with pytest.raises(ValueError):
        extract_json("no json here")


# --- the prompt asks for what the schema requires -------------------------
def test_the_analysis_prompt_names_every_required_field():
    """The client has a documented fallback that drops `output_config` entirely
    when the API or SDK rejects it. In that path the model sees no JSON schema
    at all, so the field names have to be in the prompt text -- and on a small
    model that is the difference between structured output and a shrug."""
    combined = (prompts.ANALYSIS_SYSTEM + prompts.ANALYSIS_USER_TEMPLATE).lower()
    for field in (
        "event_type", "sentiment", "market_impact", "confidence",
        "time_horizon", "tickers", "facts", "speculation",
    ):
        assert field in combined, f"{field} is in the schema but named nowhere in the prompt"


def test_the_prompt_forbids_markdown_fences():
    """Small models fence JSON by default; the parser copes, but asking is
    cheaper than stripping."""
    assert "no markdown fences" in prompts.ANALYSIS_SYSTEM.lower()
