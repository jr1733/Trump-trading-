"""The shadow model comparison.

The hard requirement is that it can never affect a signal or an alert. That is
enforced structurally -- separate table, no relationship from Event, no import
from any module that builds signals -- and both the behaviour and the structure
are asserted here. A flag on `claude_analyses` would have been less code and one
forgotten filter away from a shadow reading driving a real alert.
"""

from __future__ import annotations

import datetime as dt
import pathlib

from sqlalchemy import func, select

from app.config import settings
from app.llm.client import LLMOutcome
from app.models import ClaudeAnalysis, Event, ShadowAnalysis
from app.pipeline import analysis as analysis_mod
from app.pipeline import shadow

UTC = dt.timezone.utc


def make_event(db, suffix="1", text="Tariffs on imported semiconductors will rise."):
    event = Event(
        source_key="mock",
        external_id=f"shadow-{suffix}",
        title="Tariff statement",
        text=text,
        content_hash=f"hash-shadow-{suffix}",
        source_timestamp=dt.datetime(2026, 3, 4, 15, 0, tzinfo=UTC),
        relevant=True,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


class StubClient:
    def __init__(self, sentiment=0.2, model="claude-opus-5"):
        self.available = True
        self.sentiment = sentiment
        self.model = model
        self.models_called: list[str] = []

    def analyse(self, *, source, author, timestamp, title, text, model=None):
        used = model or self.model
        self.models_called.append(used)
        return LLMOutcome(
            status="COMPLETE",
            model=used,
            parsed={
                "event_type": "tariff",
                "sentiment": self.sentiment,
                "market_impact": 0.4,
                "confidence": 0.7,
                "time_horizon": "days",
                "tickers": [],
                "summary": "s",
                "facts": [],
                "positions": [],
                "claims": [],
                "speculation": [],
            },
            raw_response="{}",
            input_tokens=100,
            output_tokens=40,
        )


# --- off by default -------------------------------------------------------
def test_disabled_by_default(db, monkeypatch):
    monkeypatch.setattr(settings, "shadow_analysis_model", None)
    assert shadow.enabled() is False
    assert shadow.run_shadow_analysis(db, make_event(db), StubClient()) is None


def test_a_zero_sample_rate_disables_it(db, monkeypatch):
    monkeypatch.setattr(settings, "shadow_analysis_model", "claude-opus-5")
    monkeypatch.setattr(settings, "shadow_sample_rate", 0.0)
    assert shadow.enabled() is False


def test_no_extra_call_is_made_when_disabled(db, monkeypatch):
    monkeypatch.setattr(settings, "shadow_analysis_model", None)
    client = StubClient()
    shadow.run_shadow_analysis(db, make_event(db), client)
    assert client.models_called == [], "disabled must mean not one extra call"


# --- sampling -------------------------------------------------------------
def test_sampling_is_deterministic_per_event(db, monkeypatch):
    """A random draw would resample on every pipeline re-run and charge for the
    same backlog repeatedly."""
    monkeypatch.setattr(settings, "shadow_sample_rate", 0.5)
    event = make_event(db)
    decisions = {shadow.should_sample(event) for _ in range(20)}
    assert len(decisions) == 1, "the same event must always get the same answer"


def test_a_rate_of_one_samples_everything(db, monkeypatch):
    monkeypatch.setattr(settings, "shadow_sample_rate", 1.0)
    assert all(shadow.should_sample(make_event(db, str(i))) for i in range(5))


def test_the_sample_rate_is_roughly_honoured(db, monkeypatch):
    """Hashing must distribute uniformly, or a "10% sample" could be 0% or 100%."""
    monkeypatch.setattr(settings, "shadow_sample_rate", 0.3)

    class Fake:
        def __init__(self, id):
            self.id = id

    sampled = sum(1 for i in range(2000) if shadow.should_sample(Fake(f"event-{i}")))
    assert 0.25 < sampled / 2000 < 0.35, f"got {sampled / 2000:.3f}, expected about 0.30"


# --- isolation from signals ----------------------------------------------
def test_a_shadow_row_never_becomes_the_events_analysis(db, monkeypatch):
    """THE guarantee. Primary and shadow disagree sharply; the event keeps the
    primary reading and a signal built from it sees only that."""
    monkeypatch.setattr(settings, "llm_fake_mode", False)
    monkeypatch.setattr(settings, "anthropic_analysis_model", "claude-haiku-4-5")
    monkeypatch.setattr(settings, "shadow_analysis_model", "claude-opus-5")
    monkeypatch.setattr(settings, "shadow_sample_rate", 1.0)

    event = make_event(db)
    analysis_mod.analyse_event(db, event, StubClient(sentiment=0.9, model="claude-haiku-4-5"))
    db.commit()
    shadow.run_shadow_analysis(db, event, StubClient(sentiment=-0.9))
    db.commit()
    db.refresh(event)

    assert event.analysis is not None
    assert event.analysis.model == "claude-haiku-4-5"
    assert event.analysis.sentiment == 0.9, "the shadow reading must not leak in"
    assert isinstance(event.analysis, ClaudeAnalysis)

    assert db.execute(select(func.count(ShadowAnalysis.id))).scalar() == 1


def test_shadow_rows_live_in_their_own_table(db, monkeypatch):
    monkeypatch.setattr(settings, "shadow_analysis_model", "claude-opus-5")
    monkeypatch.setattr(settings, "shadow_sample_rate", 1.0)

    event = make_event(db)
    shadow.run_shadow_analysis(db, event, StubClient(sentiment=-0.5))
    db.commit()

    assert db.execute(select(func.count(ClaudeAnalysis.id))).scalar() == 0
    assert db.execute(select(func.count(ShadowAnalysis.id))).scalar() == 1


def test_no_signal_building_module_can_reach_the_shadow_table():
    """Structural, not behavioural: the isolation holds because there is no
    import path, so it cannot be broken by a forgotten filter."""
    backend = pathlib.Path(__file__).resolve().parent.parent / "app"
    for name in ("pipeline/signals.py", "pipeline/historical.py",
                 "pipeline/notifications.py", "pipeline/backtest.py"):
        source = (backend / name).read_text()
        assert "ShadowAnalysis" not in source, f"{name} must not reference shadow rows"
        assert "shadow" not in source.lower().replace("shadowed", ""), \
            f"{name} must not reference the shadow module"


def test_the_event_model_has_no_shadow_relationship():
    """`event.analysis` must not be able to resolve to a shadow row."""
    from app.models import Event as EventModel

    related = {r.key for r in EventModel.__mapper__.relationships}
    assert "shadow" not in " ".join(related).lower()
    assert EventModel.__mapper__.relationships["analysis"].mapper.class_ is ClaudeAnalysis


# --- caching and cost -----------------------------------------------------
def test_an_existing_shadow_row_is_reused(db, monkeypatch):
    monkeypatch.setattr(settings, "shadow_analysis_model", "claude-opus-5")
    monkeypatch.setattr(settings, "shadow_sample_rate", 1.0)

    event = make_event(db)
    client = StubClient(sentiment=-0.5)
    shadow.run_shadow_analysis(db, event, client)
    db.commit()
    shadow.run_shadow_analysis(db, event, client)
    db.commit()

    assert len(client.models_called) == 1, "a second run must not pay twice"
    assert db.execute(select(func.count(ShadowAnalysis.id))).scalar() == 1


def test_the_shadow_model_is_the_one_actually_called(db, monkeypatch):
    monkeypatch.setattr(settings, "shadow_analysis_model", "claude-opus-5")
    monkeypatch.setattr(settings, "shadow_sample_rate", 1.0)

    client = StubClient(model="claude-haiku-4-5")
    shadow.run_shadow_analysis(db, make_event(db), client)
    assert client.models_called == ["claude-opus-5"]


def test_shadow_cost_is_recorded(db, monkeypatch):
    monkeypatch.setattr(settings, "shadow_analysis_model", "claude-opus-5")
    monkeypatch.setattr(settings, "shadow_sample_rate", 1.0)

    row = shadow.run_shadow_analysis(db, make_event(db), StubClient())
    db.commit()
    assert row.estimated_cost_usd > 0, "a second bill must be visible as one"


# --- the comparison -------------------------------------------------------
def test_comparison_on_an_empty_database(db, monkeypatch):
    monkeypatch.setattr(settings, "shadow_analysis_model", None)
    body = shadow.comparison(db)
    assert body["compared"] == 0
    assert body["enabled"] is False
    assert body["mean_abs_sentiment_diff"] is None


def test_comparison_reports_disagreement(db, monkeypatch):
    monkeypatch.setattr(settings, "llm_fake_mode", False)
    monkeypatch.setattr(settings, "anthropic_analysis_model", "claude-haiku-4-5")
    monkeypatch.setattr(settings, "shadow_analysis_model", "claude-opus-5")
    monkeypatch.setattr(settings, "shadow_sample_rate", 1.0)

    # Two events: one where the models agree, one where they flip direction.
    agree = make_event(db, "agree", text="Tariffs rise on chips.")
    analysis_mod.analyse_event(db, agree, StubClient(0.8, "claude-haiku-4-5"))
    shadow.run_shadow_analysis(db, agree, StubClient(0.7))

    clash = make_event(db, "clash", text="Sanctions lifted on energy exports.")
    analysis_mod.analyse_event(db, clash, StubClient(0.8, "claude-haiku-4-5"))
    shadow.run_shadow_analysis(db, clash, StubClient(-0.8))
    db.commit()

    body = shadow.comparison(db)
    assert body["compared"] == 2
    assert body["direction_disagreement_pct"] == 50.0
    assert body["mean_abs_sentiment_diff"] > 0
    assert body["estimated_cost_usd"] > 0
    assert len(body["examples"]) >= 1
    # The worst disagreement is listed first.
    worst = body["examples"][0]
    assert abs(worst["primary_sentiment"] - worst["shadow_sentiment"]) > 1.0
