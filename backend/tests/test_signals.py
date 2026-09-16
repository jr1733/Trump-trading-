"""Signal calculation: components, weights, sample-size gating, labels, panel."""

from __future__ import annotations

import pytest

from app.config import settings
from app.pipeline.historical import HorizonStats
from app.pipeline.signals import compute_signal, label_for, sample_size_factor


def stats(n: int, *, median_abnormal: float = 0.0, pos: float = 50.0) -> HorizonStats:
    return HorizonStats(
        horizon="1d",
        n=n,
        median=median_abnormal,
        median_abnormal=median_abnormal,
        positive_pct=pos,
        negative_pct=100.0 - pos,
        flag="unreliable" if n < 10 else ("limited" if n < 20 else "ok"),
    )


def signal(**kwargs):
    base = dict(
        sentiment=0.0,
        sentiment_source="model",
        model_confidence=1.0,
        stats=stats(30),
        novelty_value=0.0,
        max_similarity=0.0,
        horizon="1d",
    )
    base.update(kwargs)
    return compute_signal(**base)


# --- labels ---------------------------------------------------------------
@pytest.mark.parametrize(
    "score,label",
    [
        (0.9, "STRONGLY BULLISH"),
        (0.6, "STRONGLY BULLISH"),
        (0.35, "BULLISH"),
        (0.2, "BULLISH"),
        (0.0, "NEUTRAL"),
        (-0.19, "NEUTRAL"),
        (-0.2, "BEARISH"),
        (-0.6, "STRONGLY BEARISH"),
        (-1.0, "STRONGLY BEARISH"),
    ],
)
def test_labels_follow_thresholds(score, label):
    assert label_for(score) == label


# --- sample-size gating ---------------------------------------------------
def test_sample_size_factor_bands():
    assert sample_size_factor(0) == 0.0
    assert sample_size_factor(9) == 0.0
    assert sample_size_factor(10) == 0.6
    assert sample_size_factor(19) == 0.6
    assert sample_size_factor(20) == 1.0


def test_small_sample_gives_historical_components_zero_weight():
    """N < 10 must contribute nothing, per the spec."""
    result = signal(sentiment=0.5, stats=stats(4, median_abnormal=0.10, pos=100.0))
    assert "historical" not in result.components
    assert "consistency" not in result.components
    assert "historical" not in result.weights
    assert result.sample_flag == "unreliable"
    assert any("zero weight" in note for note in result.notes)


def test_large_sample_includes_historical_components():
    result = signal(stats=stats(25, median_abnormal=0.05, pos=80.0))
    assert result.components["historical"] == pytest.approx(1.0)
    assert result.components["consistency"] == pytest.approx(0.6)
    assert result.sample_flag == "ok"


def test_limited_sample_scales_the_score_down():
    strong = signal(sentiment=0.8, stats=stats(25, median_abnormal=0.03, pos=75.0))
    limited = signal(sentiment=0.8, stats=stats(12, median_abnormal=0.03, pos=75.0))
    assert abs(limited.score) < abs(strong.score)
    assert limited.sample_flag == "limited"


# --- components -----------------------------------------------------------
def test_historical_component_is_normalised_by_the_configured_scale():
    result = signal(stats=stats(25, median_abnormal=settings.signal_return_scale / 2))
    assert result.components["historical"] == pytest.approx(0.5)


def test_historical_component_clamps_at_one():
    result = signal(stats=stats(25, median_abnormal=0.5))
    assert result.components["historical"] == 1.0


def test_consistency_is_zero_on_a_coin_flip():
    assert signal(stats=stats(25, pos=50.0)).components["consistency"] == pytest.approx(0.0)


def test_consistency_is_signed_by_the_majority_direction():
    assert signal(stats=stats(25, pos=90.0)).components["consistency"] == pytest.approx(0.8)
    assert signal(stats=stats(25, pos=10.0)).components["consistency"] == pytest.approx(-0.8)


def test_novelty_amplifies_but_never_creates_direction():
    neutral = signal(sentiment=0.0, novelty_value=1.0, stats=stats(25, median_abnormal=0.0))
    assert neutral.components["novelty"] == 0.0
    assert neutral.score == pytest.approx(0.0)

    bullish = signal(sentiment=0.5, novelty_value=1.0, stats=stats(25, median_abnormal=0.0))
    assert bullish.components["novelty"] == 1.0

    bearish = signal(sentiment=-0.5, novelty_value=1.0, stats=stats(25, median_abnormal=0.0))
    assert bearish.components["novelty"] == -1.0


def test_model_confidence_scales_the_score():
    full = signal(sentiment=0.8, model_confidence=1.0)
    half = signal(sentiment=0.8, model_confidence=0.5)
    assert half.score == pytest.approx(full.score * 0.5, abs=1e-6)


def test_score_is_bounded():
    result = signal(
        sentiment=1.0, novelty_value=1.0, stats=stats(50, median_abnormal=1.0, pos=100.0)
    )
    assert -1.0 <= result.score <= 1.0
    assert result.score == pytest.approx(1.0)


def test_weights_are_renormalised_when_components_drop_out():
    result = signal(sentiment=0.5, stats=stats(2))
    assert sum(result.weights.values()) == pytest.approx(1.0)
    assert set(result.weights) == {"sentiment", "novelty"}


# --- the mandatory "Why?" panel ------------------------------------------
def test_panel_always_carries_the_standing_uncertainties():
    result = signal(stats=stats(40))
    blob = " ".join(result.uncertainties).lower()
    assert "not independent" in blob
    assert "regime" in blob
    assert "association is not causation" in blob


def test_panel_flags_an_empty_sample():
    assert any("no comparable past events" in u.lower() for u in signal(stats=stats(0)).uncertainties)


def test_panel_flags_a_small_sample():
    assert any("fewer than" in u for u in signal(stats=stats(5)).uncertainties)


def test_panel_flags_low_model_confidence():
    result = signal(model_confidence=0.2)
    assert any("confidence in the reading is low" in u for u in result.uncertainties)


def test_panel_flags_a_rule_based_sentiment_source():
    result = signal(sentiment_source="rule_based")
    assert any("rule_based scorer" in u for u in result.uncertainties)


def test_panel_flags_the_offline_canned_scorer():
    result = signal(sentiment_source="model", sentiment_model="canned-mock")
    assert any("canned scorer" in u for u in result.uncertainties)


def test_panel_does_not_flag_a_real_model():
    result = signal(sentiment_source="model", sentiment_model="claude-opus-5")
    assert not any("canned scorer" in u for u in result.uncertainties)
    assert not any("rule_based" in u for u in result.uncertainties)


def test_panel_flags_low_confidence_ticker_links():
    result = signal(ticker_confidence="LOW")
    assert any("LOW confidence" in u for u in result.uncertainties)


def test_panel_flags_a_recent_near_duplicate():
    result = signal(max_similarity=0.85)
    assert any("already be priced in" in u for u in result.uncertainties)


def test_contributions_sum_to_the_prescaled_score():
    result = signal(sentiment=0.4, stats=stats(25, median_abnormal=0.02, pos=70.0))
    assert sum(result.contributions.values()) == pytest.approx(result.score, abs=1e-3)
