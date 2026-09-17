"""Anthropic client wrapper.

Design points:
* **Never raises into the pipeline.** Every call returns an `LLMOutcome`; the
  caller decides whether the event is PENDING, FAILED or UNAVAILABLE.
* **Structured outputs with a graceful downgrade.** We ask for
  ``output_config.format`` (JSON schema); if the API rejects that parameter we
  retry once without it and parse JSON out of the text. Either way the result is
  validated against the Pydantic model.
* **Validation retry.** One retry that feeds the validation error back, exactly
  as the spec requires, then FAILED.
* **Token accounting.** Every call returns usage so `llm_usage` can be written.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from . import prompts
from .schema import (
    JSON_SCHEMA,
    TRIAGE_SCHEMA,
    AnalysisResult,
    TriageResult,
    ValidationError,
)

log = logging.getLogger(__name__)

# Published rates, USD per million tokens. Used only for the cost estimate shown
# in the admin view -- see README "Cost control".
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_DEFAULT_PRICING = (5.0, 25.0)

#: Models that accept `output_config.effort`. An ALLOWLIST rather than a
#: denylist, because the failure modes are not symmetric: guessing that an
#: unknown model supports effort costs a 400 on every analysis call, while
#: guessing it does not costs only the effort setting. Haiku 4.5 rejects effort
#: outright -- which matters here because Haiku is the cheapest-configuration
#: default, so the obvious cost-saving change would otherwise break every call.
_EFFORT_SUPPORTED_PREFIXES = (
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-sonnet-5",
    "claude-fable-5",
)


def _normalise_model(model: str) -> str:
    """Strip a dated snapshot suffix: claude-haiku-4-5-20251001 -> claude-haiku-4-5.

    Both the dated and undated forms are valid model IDs. Without this the
    pricing table misses every dated ID and silently falls back to the Opus
    rate, overstating a Haiku bill by 5x on the cost page -- which is exactly
    the number you would be looking at to decide whether Haiku was worth it.
    """
    return re.sub(r"-\d{8}$", "", (model or "").strip())


def supports_effort(model: str) -> bool:
    return _normalise_model(model).startswith(_EFFORT_SUPPORTED_PREFIXES)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = MODEL_PRICING.get(
        _normalise_model(model), _DEFAULT_PRICING
    )
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object out of a model response.

    Handles the two things that actually happen in practice: markdown fences,
    and a JSON object with leading/trailing prose.
    """
    cleaned = _FENCE.sub("", (text or "").strip()).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise
        parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model returned JSON that is not an object")
    return parsed


@dataclass
class LLMOutcome:
    """Result of one logical LLM operation (which may span a validation retry)."""

    status: str  # COMPLETE | FAILED | UNAVAILABLE
    parsed: dict[str, Any] | None = None
    raw_response: str | None = None
    error: str | None = None
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    attempts: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "COMPLETE"

    @property
    def cost(self) -> float:
        return estimate_cost(self.model, self.input_tokens, self.output_tokens)


class AnthropicClient:
    """Thin wrapper. Pass `client=` in tests to inject a fake."""

    def __init__(self, client: Any = None) -> None:
        self._client = client
        self._supports_output_config = True
        if client is None and settings.llm_enabled:
            try:
                import anthropic

                self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            except Exception as exc:  # pragma: no cover - import/credential issues
                log.error("could not construct Anthropic client: %s", exc)
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    # -- raw call ---------------------------------------------------------
    def _call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        json_schema: dict,
        max_tokens: int,
        effort: str | None = None,
    ) -> tuple[str, dict[str, int]]:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        output_config: dict[str, Any] = {}
        # Effort is model-gated. Haiku 4.5 rejects it outright, and Haiku is the
        # cheapest-configuration default, so sending it unconditionally would
        # mean every analysis call 400s the moment someone follows the cost
        # advice in the README.
        if effort and supports_effort(model):
            output_config["effort"] = effort
        elif effort:
            log.debug("model %s does not accept effort; omitting it", model)
        if self._supports_output_config:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        if output_config:
            kwargs["output_config"] = output_config

        def _send() -> Any:
            return self._client.messages.create(**kwargs)

        def _drop_effort() -> bool:
            """Remove effort but keep the JSON schema. Returns whether anything
            changed -- losing structured output because of an effort complaint
            would trade a tuning knob for the guarantee the parser relies on."""
            config = kwargs.get("output_config") or {}
            if "effort" not in config:
                return False
            config.pop("effort")
            if config:
                kwargs["output_config"] = config
            else:
                kwargs.pop("output_config", None)
            return True

        try:
            response = _send()
        except TypeError as exc:
            # An SDK too old to know `output_config`: drop it and never try again.
            if "output_config" not in str(exc) or not self._supports_output_config:
                raise
            log.warning("SDK rejected output_config; falling back to prompt-only JSON")
            self._supports_output_config = False
            kwargs.pop("output_config", None)
            response = _send()
        except Exception as exc:
            message = str(exc)
            if "effort" in message and _drop_effort():
                log.warning(
                    "model %s rejected effort (%s); retrying with the schema intact",
                    model,
                    message[:120],
                )
                response = _send()
            elif self._supports_output_config and "output_config" in message:
                log.warning("API rejected output_config (%s); retrying without it", message[:120])
                self._supports_output_config = False
                kwargs.pop("output_config", None)
                response = _send()
            else:
                raise

        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        usage = getattr(response, "usage", None)
        counts = {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "cache_read_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        }
        return text, counts

    # -- triage -----------------------------------------------------------
    def triage(self, *, source: str, title: str | None, text: str) -> LLMOutcome:
        model = settings.anthropic_triage_model
        if not self.available:
            return LLMOutcome(status="UNAVAILABLE", error="no Anthropic API key", model=model)
        user = prompts.triage_user_prompt(source=source, title=title, text=text)
        try:
            raw, usage = self._call(
                model=model,
                system=prompts.TRIAGE_SYSTEM,
                user=user,
                json_schema=TRIAGE_SCHEMA,
                max_tokens=256,
            )
        except Exception as exc:
            log.warning("triage call failed: %s", exc)
            return LLMOutcome(status="FAILED", error=str(exc), model=model, attempts=1)

        try:
            parsed = TriageResult.model_validate(extract_json(raw))
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            return LLMOutcome(
                status="FAILED", raw_response=raw, error=str(exc), model=model, attempts=1, **usage
            )
        return LLMOutcome(
            status="COMPLETE",
            parsed=parsed.model_dump(),
            raw_response=raw,
            model=model,
            attempts=1,
            **usage,
        )

    # -- full analysis ----------------------------------------------------
    def analyse(
        self,
        *,
        source: str,
        author: str | None,
        timestamp: str,
        title: str | None,
        text: str,
        model: str | None = None,
    ) -> LLMOutcome:
        # `model` is an override for the shadow comparison run. Everything else
        # uses the configured analysis model.
        model = model or settings.anthropic_analysis_model
        if not self.available:
            return LLMOutcome(status="UNAVAILABLE", error="no Anthropic API key", model=model)

        user = prompts.analysis_user_prompt(
            source=source, author=author, timestamp=timestamp, title=title, text=text
        )
        totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        last_raw: str | None = None
        last_error: str | None = None

        for attempt in range(settings.anthropic_max_analysis_retries + 1):
            try:
                raw, usage = self._call(
                    model=model,
                    system=prompts.ANALYSIS_SYSTEM,
                    user=user,
                    json_schema=JSON_SCHEMA,
                    max_tokens=2048,
                    effort=settings.anthropic_analysis_effort,
                )
            except Exception as exc:
                log.warning("analysis call failed (attempt %d): %s", attempt + 1, exc)
                return LLMOutcome(
                    status="FAILED",
                    error=str(exc),
                    model=model,
                    attempts=attempt + 1,
                    raw_response=last_raw,
                    **totals,
                )

            for key in totals:
                totals[key] += usage[key]
            last_raw = raw

            try:
                parsed = AnalysisResult.model_validate(extract_json(raw))
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                log.info("analysis failed validation (attempt %d): %s", attempt + 1, last_error)
                # Feed the validation error back once, exactly as specified.
                user = prompts.analysis_user_prompt(
                    source=source, author=author, timestamp=timestamp, title=title, text=text
                ) + prompts.RETRY_SUFFIX.format(error=last_error[:1000])
                continue

            return LLMOutcome(
                status="COMPLETE",
                parsed=parsed.model_dump(),
                raw_response=raw,
                model=model,
                attempts=attempt + 1,
                **totals,
            )

        return LLMOutcome(
            status="FAILED",
            raw_response=last_raw,
            error=last_error,
            model=model,
            attempts=settings.anthropic_max_analysis_retries + 1,
            **totals,
        )
