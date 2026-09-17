"""The analysis cache key.

The bug this file exists for: `claude_analyses` used to be UNIQUE(content_hash)
and `cached_analysis()` looked the row up by content hash alone. So the first
COMPLETE analysis of a piece of text won forever -- including a canned
`LLM_FAKE_MODE` placeholder. Run a week offline to shake out the feeds (which is
exactly what the deployment guide recommends), then turn the real model on, and
every event from that week keeps its placeholder analysis. No error, no cost, no
sign of it in the UI.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from app.config import settings
from app.llm.client import LLMOutcome
from app.models import ClaudeAnalysis, Event
from app.pipeline import analysis as analysis_mod

UTC = dt.timezone.utc


def make_event(db, text="Tariffs on imported semiconductors will rise."):
    event = Event(
        source_key="mock",
        external_id="cache-1",
        title="Tariff statement",
        text=text,
        content_hash="hash-cache-1",
        source_timestamp=dt.datetime(2026, 3, 4, 15, 0, tzinfo=UTC),
        relevant=True,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


class CountingClient:
    """A stand-in for AnthropicClient that records every call it receives."""

    def __init__(self, model="claude-haiku-4-5-20251001", sentiment=0.7):
        self.available = True
        self.model = model
        self.sentiment = sentiment
        self.calls: list[str] = []

    def analyse(self, *, source, author, timestamp, title, text):
        self.calls.append(text)
        return LLMOutcome(
            status="COMPLETE",
            model=self.model,
            parsed={
                "event_type": "tariff",
                "sentiment": self.sentiment,
                "market_impact": 0.5,
                "confidence": 0.8,
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
            output_tokens=50,
        )


# --- the regression -------------------------------------------------------
def test_switching_from_fake_to_real_mode_reanalyses(db, monkeypatch):
    """THE test. Analyse in fake mode, switch to real, assert a real call."""
    event = make_event(db)

    # 1. A week offline: canned analysis, no model involved.
    monkeypatch.setattr(settings, "llm_fake_mode", True)
    fake = CountingClient(model="canned-mock", sentiment=0.1)
    analysis_mod.analyse_event(db, event, fake)
    db.commit()
    db.refresh(event)

    assert len(fake.calls) == 1
    assert event.analysis.model == "canned-mock"
    assert event.analysis.mode == "fake"

    # 2. Real mode, real model.
    monkeypatch.setattr(settings, "llm_fake_mode", False)
    monkeypatch.setattr(settings, "anthropic_analysis_model", "claude-haiku-4-5-20251001")
    real = CountingClient(model="claude-haiku-4-5-20251001", sentiment=0.7)
    analysis_mod.analyse_event(db, event, real)
    db.commit()
    db.refresh(event)

    assert real.calls, "switching to real mode must re-analyse, not reuse the canned row"
    assert event.analysis.model == "claude-haiku-4-5-20251001"
    assert event.analysis.mode == "live"
    assert event.analysis.sentiment == 0.7, "the signal must see the real reading"


def test_the_canned_row_survives_as_its_own_cache_entry(db, monkeypatch):
    """Re-analysis detaches the old row; it does not delete it."""
    event = make_event(db)

    monkeypatch.setattr(settings, "llm_fake_mode", True)
    analysis_mod.analyse_event(db, event, CountingClient(model="canned-mock"))
    db.commit()

    monkeypatch.setattr(settings, "llm_fake_mode", False)
    monkeypatch.setattr(settings, "anthropic_analysis_model", "claude-haiku-4-5-20251001")
    analysis_mod.analyse_event(db, event, CountingClient(model="claude-haiku-4-5-20251001"))
    db.commit()

    rows = list(db.execute(select(ClaudeAnalysis)).scalars())
    assert len(rows) == 2, "both readings are kept"
    attached = [r for r in rows if r.event_id == event.id]
    assert len(attached) == 1, "but exactly one is attached to the event"
    assert attached[0].mode == "live"


def test_the_cache_still_works_within_one_model_and_mode(db, monkeypatch):
    """The fix must not break what the cache was for: identical text, same
    model, same mode, analysed twice, costs one call."""
    monkeypatch.setattr(settings, "llm_fake_mode", False)
    monkeypatch.setattr(settings, "anthropic_analysis_model", "claude-haiku-4-5-20251001")
    event = make_event(db)
    client = CountingClient()

    analysis_mod.analyse_event(db, event, client)
    db.commit()
    analysis_mod.analyse_event(db, event, client)
    db.commit()

    assert len(client.calls) == 1, "the second call must be served from cache"


def test_the_lookup_discriminates_on_all_three_parts_of_the_key(db, monkeypatch):
    """Direct test of the key. (Two *events* can never share a content hash --
    `events` is itself UNIQUE(content_hash) and cross-source duplicates collapse
    upstream -- so this exercises the lookup rather than a second event.)"""
    monkeypatch.setattr(settings, "llm_fake_mode", False)
    monkeypatch.setattr(settings, "anthropic_analysis_model", "claude-haiku-4-5-20251001")
    event = make_event(db)
    analysis_mod.analyse_event(db, event, CountingClient())
    db.commit()

    found = analysis_mod.cached_analysis(db, event.content_hash)
    assert found is not None, "same hash, same model, same mode -> hit"

    assert analysis_mod.cached_analysis(db, "a-different-hash") is None
    assert analysis_mod.cached_analysis(db, event.content_hash, model="claude-opus-5") is None
    assert analysis_mod.cached_analysis(db, event.content_hash, mode="fake") is None


def test_changing_model_reanalyses_within_live_mode(db, monkeypatch):
    """Model is in the key too: a cheaper model must not inherit the expensive
    one's reading, nor the other way round."""
    monkeypatch.setattr(settings, "llm_fake_mode", False)
    event = make_event(db)

    monkeypatch.setattr(settings, "anthropic_analysis_model", "claude-haiku-4-5-20251001")
    analysis_mod.analyse_event(db, event, CountingClient(model="claude-haiku-4-5-20251001"))
    db.commit()

    monkeypatch.setattr(settings, "anthropic_analysis_model", "claude-opus-5")
    opus = CountingClient(model="claude-opus-5", sentiment=-0.4)
    analysis_mod.analyse_event(db, event, opus)
    db.commit()
    db.refresh(event)

    assert opus.calls, "a different model must actually be called"
    assert event.analysis.model == "claude-opus-5"
    assert event.analysis.sentiment == -0.4


def test_mode_is_recorded_on_every_row(db, monkeypatch):
    monkeypatch.setattr(settings, "llm_fake_mode", True)
    event = make_event(db)
    analysis_mod.analyse_event(db, event, CountingClient(model="canned-mock"))
    db.commit()

    modes = set(db.execute(select(ClaudeAnalysis.mode)).scalars())
    assert modes == {"fake"}
    assert db.execute(select(func.count(ClaudeAnalysis.id))).scalar() == 1
