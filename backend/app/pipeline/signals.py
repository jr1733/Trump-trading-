"""Signal calculation.

    score = w1*sentiment
          + w2*historical_median_abnormal_return_normalised
          + w3*historical_consistency
          + w4*novelty_signed
    score = score * model_confidence * sample_size_factor

Component definitions -- these are the definitions, the README repeats them, and
the "Why?" panel shows each computed value:

* **sentiment** -- the model's `sentiment` field, already in [-1, 1]. When no
  model analysis exists, the rule-based lexicon score stands in and the panel
  says so.
* **historical** -- median abnormal return over comparable past events at the
  primary horizon, divided by `SIGNAL_RETURN_SCALE` (default 0.05, i.e. a 5%
  median abnormal move is full scale), clamped to [-1, 1].
* **consistency** -- how one-sided the historical sample is, *signed in the
  direction of the median*: `2 * max(pos%, neg%)/100 - 1`, so a 50/50 split
  contributes 0 and a 90/10 split contributes 0.8. It is 0 when N == 0.
* **novelty** -- `1 - max_similarity_to_the_last_30_days`, signed in the
  direction the other components already point. Novelty has no direction of its
  own: a novel event is not bullish, it is merely less anticipated, so it can
  only amplify an existing lean, never create one.
* **model confidence** -- multiplies the whole score. A 0.3-confidence reading
  cannot produce a strong label.
* **sample-size factor** -- `0.0` when the sample is unreliable (N < 10, so the
  historical terms get **zero weight** as the spec requires), `0.6` when limited
  (10 <= N < 20), `1.0` at N >= 20.

**Text-only mode (N < 10).** When the historical sample is unreliable the score
collapses to *sentiment alone*, capped in magnitude at `SIGNAL_TEXT_ONLY_CAP`
(default 0.4).

Novelty is dropped along with the historical terms, and this is the point of the
rule rather than an oversight. Novelty is defined as an amplifier of an existing
lean, and with no usable history the only thing left to lean on is the model's
reading of the text -- so letting novelty through would amplify a text reading
with itself and dress up one opinion as two agreeing components. The cap exists
for the same reason: with no comparable past events, a confident-sounding
sentence is the entire basis for the number, and a basis that thin must not be
able to produce a STRONGLY BULLISH or STRONGLY BEARISH label (|0.6|). Such
signals are also excluded from firing threshold alerts -- see
`pipeline.notifications.evaluate_threshold_crossing`.

Market context is deliberately absent in Phase 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import settings
from .historical import HorizonStats


@dataclass
class SignalResult:
    score: float
    label: str
    confidence: float
    sample_size: int
    sample_flag: str
    horizon: str
    components: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    contributions: dict[str, float] = field(default_factory=dict)
    uncertainties: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "label": self.label,
            "confidence": self.confidence,
            "sample_size": self.sample_size,
            "sample_flag": self.sample_flag,
            "horizon": self.horizon,
            "components": self.components,
            "weights": self.weights,
            "contributions": self.contributions,
            "uncertainties": self.uncertainties,
            "notes": self.notes,
        }


def label_for(score: float) -> str:
    thresholds = settings.signal_thresholds()
    if score >= thresholds["strongly_bullish"]:
        return "STRONGLY BULLISH"
    if score >= thresholds["bullish"]:
        return "BULLISH"
    if score <= thresholds["strongly_bearish"]:
        return "STRONGLY BEARISH"
    if score <= thresholds["bearish"]:
        return "BEARISH"
    return "NEUTRAL"


def sample_size_factor(n: int) -> float:
    if n < settings.min_usable_sample:
        return 0.0
    if n < settings.min_reliable_sample:
        return 0.6
    return 1.0


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def compute_signal(
    *,
    sentiment: float,
    sentiment_source: str,
    model_confidence: float,
    stats: HorizonStats | None,
    novelty_value: float,
    max_similarity: float,
    horizon: str,
    ticker_confidence: str = "HIGH",
    sentiment_model: str | None = None,
) -> SignalResult:
    weights = settings.signal_weights()
    n = stats.n if stats else 0
    flag = stats.flag if stats else "unreliable"
    size_factor = sample_size_factor(n)

    components: dict[str, float] = {"sentiment": round(_clamp(sentiment), 4)}
    uncertainties: list[str] = []
    notes: list[str] = []

    historical_usable = stats is not None and size_factor > 0.0
    if historical_usable:
        median_abn = stats.median_abnormal
        if median_abn is None:
            median_abn = stats.median or 0.0
            notes.append(
                "No benchmark data for the comparable events; the historical "
                "component uses raw returns rather than abnormal returns."
            )
        components["historical"] = round(
            _clamp(median_abn / settings.signal_return_scale), 4
        )
        pos = (stats.positive_pct or 0.0) / 100.0
        neg = (stats.negative_pct or 0.0) / 100.0
        magnitude = 2.0 * max(pos, neg) - 1.0
        direction = 1.0 if pos >= neg else -1.0
        components["consistency"] = round(_clamp(magnitude * direction), 4)
    else:
        notes.append(
            f"Historical sample is too small (N={n} < {settings.min_usable_sample}); "
            "the historical and consistency components are given zero weight."
        )

    if historical_usable:
        # Novelty amplifies the existing lean; it never creates one.
        directional = [components.get("sentiment", 0.0), components.get("historical", 0.0)]
        lean = sum(directional)
        novelty_sign = 1.0 if lean > 0 else (-1.0 if lean < 0 else 0.0)
        components["novelty"] = round(_clamp(novelty_value * novelty_sign), 4)
    else:
        # Text-only mode: novelty would amplify the sentiment reading with
        # itself, presenting one opinion as two agreeing components.
        notes.append(
            "Novelty is also dropped: with no usable history it would only "
            "amplify the text reading with itself."
        )

    active = {k: w for k, w in weights.items() if k in components}
    total_weight = sum(active.values()) or 1.0
    normalised = {k: w / total_weight for k, w in active.items()}

    contributions = {k: round(normalised[k] * components[k], 4) for k in active}
    base = sum(contributions.values())
    score = _clamp(base * _clamp(model_confidence, 0.0, 1.0) * (size_factor if historical_usable else 1.0))

    if not historical_usable:
        # The whole number rests on one reading of one piece of text. Cap it so
        # it cannot reach a STRONG label on that basis alone.
        cap = settings.signal_text_only_cap
        if abs(score) > cap:
            score = cap if score > 0 else -cap
            notes.append(
                f"Score capped at {cap:+.2f} because it rests on the text reading "
                "alone. A text reading with no comparable history cannot produce a "
                "strong label."
            )

    # --- mandatory uncertainties -----------------------------------------
    if n == 0:
        uncertainties.append(
            "No comparable past events were found. This score rests on the text "
            f"reading alone, is capped at {settings.signal_text_only_cap:.2f}, and "
            "cannot raise a threshold alert."
        )
    elif flag == "unreliable":
        uncertainties.append(
            f"Only {n} comparable past events (fewer than {settings.min_usable_sample}); "
            "the historical statistics are shown but carry no weight in the score. "
            f"The score is capped at {settings.signal_text_only_cap:.2f} and cannot "
            "raise a threshold alert."
        )
    elif flag == "limited":
        uncertainties.append(
            f"{n} comparable past events is a limited sample "
            f"(fewer than {settings.min_reliable_sample}); treat the statistics as indicative."
        )
    uncertainties.append(
        "Comparable events are not independent observations: they cluster in time "
        "and often share an underlying cause, so the effective sample is smaller "
        "than N suggests."
    )
    uncertainties.append(
        "Policy, rates and market regime change over the sample period. Past "
        "reactions were produced under conditions that no longer hold."
    )
    uncertainties.append(
        "These are statistical associations between past announcements and past "
        "price moves. Association is not causation, and none of this is a forecast."
    )
    if sentiment_source != "model":
        uncertainties.append(
            f"Sentiment came from the {sentiment_source} scorer rather than a full "
            "model analysis, which is coarser."
        )
    elif sentiment_model == "canned-mock":
        uncertainties.append(
            "This analysis came from the offline canned scorer (LLM_FAKE_MODE), not "
            "from a language model. It is placeholder structure, not a reading of the text."
        )
    if model_confidence < 0.4:
        uncertainties.append(
            f"Model confidence in the reading is low ({model_confidence:.2f}); the "
            "score is scaled down accordingly."
        )
    if ticker_confidence == "LOW":
        uncertainties.append(
            "The link between this event and this ticker is LOW confidence, so the "
            "event may not be about this company at all."
        )
    if max_similarity > 0.7:
        uncertainties.append(
            f"A very similar event occurred in the last 30 days (similarity "
            f"{max_similarity:.2f}); much of this may already be priced in."
        )

    return SignalResult(
        score=round(score, 4),
        label=label_for(score),
        confidence=round(_clamp(model_confidence, 0.0, 1.0), 4),
        sample_size=n,
        sample_flag=flag,
        horizon=horizon,
        components=components,
        weights={k: round(v, 4) for k, v in normalised.items()},
        contributions=contributions,
        uncertainties=uncertainties,
        notes=notes,
    )
