"""Claude JSON validation, the single retry, and failure handling."""

from __future__ import annotations

import json

import pytest

from app.llm.client import AnthropicClient, extract_json
from app.llm.schema import AnalysisResult, ValidationError

VALID = {
    "event_type": "tariff",
    "entities": ["Apple Inc."],
    "tickers": ["aapl", "$NVDA"],
    "sentiment": -0.4,
    "market_impact": -0.3,
    "confidence": 0.6,
    "time_horizon": "days",
    "reasoning": "text",
    "facts": ["a fact"],
    "stated_positions": [],
    "third_party_claims": [],
    "speculation": [],
    "uncertainty": ["no rate stated"],
}


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Usage:
    input_tokens = 100
    output_tokens = 50
    cache_read_input_tokens = 0


class _Response:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]
        self.usage = _Usage()


class FakeMessages:
    """Returns each queued response in turn, recording the prompts it was sent."""

    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Response(item)


class FakeSDK:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.messages = FakeMessages(responses)


def client_for(responses: list[str | Exception]) -> AnthropicClient:
    return AnthropicClient(client=FakeSDK(responses))


# --- schema validation ----------------------------------------------------
def test_valid_payload_normalises_tickers():
    result = AnalysisResult.model_validate(VALID)
    assert result.tickers == ["AAPL", "NVDA"]


def test_sentiment_out_of_range_is_rejected():
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate({**VALID, "sentiment": 1.7})


def test_confidence_out_of_range_is_rejected():
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate({**VALID, "confidence": -0.1})


def test_unknown_event_type_falls_back_to_other():
    assert AnalysisResult.model_validate({**VALID, "event_type": "vibes"}).event_type == "other"


def test_unknown_time_horizon_is_rejected():
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate({**VALID, "time_horizon": "fortnight"})


# --- response parsing -----------------------------------------------------
def test_extract_json_handles_markdown_fences():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_handles_surrounding_prose():
    assert extract_json('Sure, here you go:\n{"a": 1}\nHope that helps.') == {"a": 1}


def test_extract_json_rejects_non_objects():
    with pytest.raises(ValueError):
        extract_json("[1, 2, 3]")


# --- the retry contract ---------------------------------------------------
def test_first_response_valid_means_one_call():
    client = client_for([json.dumps(VALID)])
    outcome = client.analyse(
        source="mock", author="a", timestamp="2026-03-04T15:00:00+00:00", title="t", text="x"
    )
    assert outcome.status == "COMPLETE"
    assert outcome.attempts == 1
    assert outcome.parsed["tickers"] == ["AAPL", "NVDA"]
    assert outcome.input_tokens == 100


def test_malformed_output_retries_once_with_the_validation_error():
    client = client_for(["not json at all", json.dumps(VALID)])
    outcome = client.analyse(
        source="mock", author="a", timestamp="2026-03-04T15:00:00+00:00", title="t", text="x"
    )
    assert outcome.status == "COMPLETE"
    assert outcome.attempts == 2
    # Token usage accumulates across the retry.
    assert outcome.input_tokens == 200

    second_prompt = client._client.messages.calls[1]["messages"][0]["content"]
    assert "failed schema validation" in second_prompt


def test_two_malformed_outputs_mark_failed():
    client = client_for(["nope", "still nope"])
    outcome = client.analyse(
        source="mock", author="a", timestamp="2026-03-04T15:00:00+00:00", title="t", text="x"
    )
    assert outcome.status == "FAILED"
    assert outcome.attempts == 2
    assert outcome.error
    assert outcome.raw_response == "still nope"


def test_out_of_range_value_triggers_the_retry_path():
    bad = json.dumps({**VALID, "market_impact": 9.9})
    client = client_for([bad, json.dumps(VALID)])
    outcome = client.analyse(
        source="mock", author="a", timestamp="2026-03-04T15:00:00+00:00", title="t", text="x"
    )
    assert outcome.status == "COMPLETE"
    assert outcome.attempts == 2


def test_api_exception_is_reported_not_raised():
    client = client_for([RuntimeError("503 upstream unavailable")])
    outcome = client.analyse(
        source="mock", author="a", timestamp="2026-03-04T15:00:00+00:00", title="t", text="x"
    )
    assert outcome.status == "FAILED"
    assert "503" in outcome.error


def test_missing_api_key_reports_unavailable():
    client = AnthropicClient(client=None)
    outcome = client.analyse(
        source="mock", author="a", timestamp="2026-03-04T15:00:00+00:00", title="t", text="x"
    )
    assert outcome.status == "UNAVAILABLE"
    assert outcome.parsed is None


def test_output_config_rejection_falls_back_once():
    """If the API rejects output_config we retry without it and stop sending it."""

    class PickyMessages(FakeMessages):
        def create(self, **kwargs):
            if "output_config" in kwargs:
                raise RuntimeError("unexpected parameter: output_config")
            return super().create(**kwargs)

    sdk = FakeSDK([json.dumps(VALID)])
    sdk.messages = PickyMessages([json.dumps(VALID)])
    client = AnthropicClient(client=sdk)

    outcome = client.analyse(
        source="mock", author="a", timestamp="2026-03-04T15:00:00+00:00", title="t", text="x"
    )
    assert outcome.status == "COMPLETE"
    assert client._supports_output_config is False


def test_triage_parses_and_reports_usage():
    client = client_for([json.dumps({"relevant": True, "score": 0.8, "reason": "tariffs"})])
    outcome = client.triage(source="mock", title="t", text="tariffs on chips")
    assert outcome.ok
    assert outcome.parsed["relevant"] is True
    assert outcome.cost > 0
