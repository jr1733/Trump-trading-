"""Embeddings: vector storage, similarity search, novelty, look-ahead."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.embeddings.base import cosine_similarity, l2_normalise
from app.embeddings.hashing import HashingEmbeddingProvider
from app.embeddings.service import EmbeddingService, build_provider, vector_literal
from app.models import Event, EventEmbedding, EventTicker, Ticker

UTC = dt.timezone.utc


def add_event(db, *, text, when=None, title="Statement", ticker=None, event_type="tariff"):
    import hashlib

    # The column is 64 chars, so hash rather than concatenate.
    digest = hashlib.sha256(f"{title}|{text}|{when}".encode()).hexdigest()
    event = Event(
        source_key="mock",
        external_id=f"e-{text[:24]}-{when}",
        title=title,
        text=text,
        content_hash=digest,
        event_type=event_type,
        source_timestamp=when or dt.datetime(2026, 3, 4, 15, 0, tzinfo=UTC),
        relevant=True,
    )
    db.add(event)
    db.flush()
    if ticker:
        db.add(EventTicker(event_id=event.id, ticker=ticker, confidence="HIGH"))
    db.commit()
    db.refresh(event)
    return event


# --- vector maths ---------------------------------------------------------
def test_l2_normalise_produces_unit_vectors():
    vector = l2_normalise([3.0, 4.0])
    assert pytest.approx(sum(v * v for v in vector)) == 1.0


def test_l2_normalise_leaves_a_zero_vector_alone():
    assert l2_normalise([0.0, 0.0]) == [0.0, 0.0]


def test_cosine_similarity_bounds():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0], []) == 0.0


def test_vector_literal_handles_non_python_floats():
    """Vectors read back from pgvector are numpy float32, whose repr is not
    valid pgvector input."""

    class FakeFloat(float):
        def __repr__(self):
            return f"np.float32({float(self)})"

    literal = vector_literal([FakeFloat(0.5), FakeFloat(-0.25)])
    assert literal == "[0.5,-0.25]"
    assert "np.float32" not in literal


# --- the hashing provider -------------------------------------------------
def test_hashing_provider_is_deterministic():
    a = HashingEmbeddingProvider(64).embed_one("tariffs on imported semiconductors")
    b = HashingEmbeddingProvider(64).embed_one("tariffs on imported semiconductors")
    assert a == b


def test_hashing_provider_emits_the_configured_dimension():
    provider = HashingEmbeddingProvider(128)
    assert len(provider.embed_one("hello world")) == 128


def test_hashing_vectors_are_normalised():
    vector = HashingEmbeddingProvider(64).embed_one("tariffs on chips")
    assert pytest.approx(sum(v * v for v in vector), abs=1e-9) == 1.0


def test_related_text_scores_above_unrelated_text():
    provider = HashingEmbeddingProvider(384)
    subject = provider.embed_one("Tariffs on imported semiconductors are under review.")
    related = provider.embed_one("A review of tariffs on imported semiconductor products.")
    unrelated = provider.embed_one("Played a round of golf in beautiful weather today.")
    assert cosine_similarity(subject, related) > cosine_similarity(subject, unrelated)


def test_empty_text_gives_a_zero_vector_not_a_crash():
    vector = HashingEmbeddingProvider(64).embed_one("")
    assert len(vector) == 64
    assert all(value == 0.0 for value in vector)


def test_hashing_provider_is_honest_about_being_lexical():
    provider = HashingEmbeddingProvider(64)
    assert provider.semantic is False
    assert "not meaning" in provider.label


def test_build_provider_defaults_to_hashing_and_falls_back():
    assert build_provider("hashing").name == "hashing"
    assert build_provider("no-such-provider").name == "hashing"


# --- storage --------------------------------------------------------------
def test_embed_event_stores_a_vector(db):
    event = add_event(db, text="Tariffs on chips are under review.")
    service = EmbeddingService(db)
    row = service.embed_event(event)
    db.commit()

    assert row.dim == settings.embedding_dim
    assert row.provider == "hashing"
    assert row.content_hash == event.content_hash
    assert len(list(row.embedding)) == settings.embedding_dim


def test_re_embedding_replaces_rather_than_duplicates(db):
    event = add_event(db, text="Tariffs on chips are under review.")
    service = EmbeddingService(db)
    service.embed_event(event)
    service.embed_event(event)
    db.commit()
    assert db.execute(select(func.count(EventEmbedding.event_id))).scalar() == 1


def test_backfill_is_idempotent(db):
    for i in range(3):
        add_event(db, text=f"Tariffs on chips, statement {i}.", title=f"S{i}")
    service = EmbeddingService(db)

    assert service.backfill() == 3
    db.commit()
    assert service.backfill() == 0, "a second pass must find nothing pending"


def test_a_provider_change_marks_vectors_stale(db):
    event = add_event(db, text="Tariffs on chips are under review.")
    service = EmbeddingService(db)
    service.embed_event(event)
    db.commit()

    # Simulate a vector written by a different provider.
    row = db.get(EventEmbedding, event.id)
    row.provider = "some-other-model"
    db.commit()

    pending = service.pending_events()
    assert event.id in {e.id for e in pending}, (
        "a vector from another embedding space is not comparable and must be redone"
    )


def test_changed_content_marks_the_vector_stale(db):
    event = add_event(db, text="Tariffs on chips are under review.")
    service = EmbeddingService(db)
    service.embed_event(event)
    db.commit()

    event.content_hash = "a-different-hash"
    db.commit()
    assert event.id in {e.id for e in service.pending_events()}


def test_vector_for_returns_none_on_a_provider_mismatch(db):
    event = add_event(db, text="Tariffs on chips.")
    service = EmbeddingService(db)
    service.embed_event(event)
    db.commit()
    row = db.get(EventEmbedding, event.id)
    row.provider = "elsewhere"
    db.commit()
    assert service.vector_for(event) is None


# --- similarity search ----------------------------------------------------
@pytest.fixture
def corpus(db):
    db.add(Ticker(symbol="NVDA", name="NVIDIA", sector="Technology", sector_etf="XLK"))
    db.commit()
    events = {
        "subject": add_event(
            db,
            text="Tariffs on imported semiconductors are under review by the administration.",
            when=dt.datetime(2026, 6, 1, 15, 0, tzinfo=UTC),
            title="Semiconductor tariffs",
            ticker="NVDA",
        ),
        "related_before": add_event(
            db,
            text="A review of tariffs on imported semiconductors was announced.",
            when=dt.datetime(2026, 5, 1, 15, 0, tzinfo=UTC),
            title="Semiconductor tariff review",
            ticker="NVDA",
        ),
        "unrelated_before": add_event(
            db,
            text="Played a wonderful round of golf in beautiful weather with great people.",
            when=dt.datetime(2026, 5, 15, 15, 0, tzinfo=UTC),
            title="Golf",
            event_type="other",
        ),
        "related_after": add_event(
            db,
            text="Another review of tariffs on imported semiconductors was announced.",
            when=dt.datetime(2026, 7, 1, 15, 0, tzinfo=UTC),
            title="More semiconductor tariffs",
            ticker="NVDA",
        ),
    }
    service = EmbeddingService(db)
    service.backfill()
    db.commit()
    return events, service


def test_similar_events_ranks_related_text_first(corpus):
    events, service = corpus
    matches = service.similar_events(events["subject"], threshold=-1.0)
    assert matches
    assert matches[0].event.id == events["related_before"].id


def test_similar_events_never_returns_the_future(corpus):
    """The load-bearing property: a similarity search must not see later events."""
    events, service = corpus
    matches = service.similar_events(events["subject"], threshold=-1.0)
    returned = {m.event.id for m in matches}
    assert events["related_after"].id not in returned
    assert all(
        m.event.source_timestamp < events["subject"].source_timestamp for m in matches
    )


def test_similar_events_excludes_the_subject_itself(corpus):
    events, service = corpus
    matches = service.similar_events(events["subject"], threshold=-1.0)
    assert events["subject"].id not in {m.event.id for m in matches}


def test_similar_events_respects_the_threshold(corpus):
    events, service = corpus
    strict = service.similar_events(events["subject"], threshold=0.99)
    assert strict == []


def test_similar_events_can_filter_by_ticker(corpus):
    events, service = corpus
    matches = service.similar_events(events["subject"], ticker="NVDA", threshold=-1.0)
    assert events["unrelated_before"].id not in {m.event.id for m in matches}


def test_similar_events_can_filter_by_event_type(corpus):
    events, service = corpus
    matches = service.similar_events(events["subject"], event_type="other", threshold=-1.0)
    assert {m.event.id for m in matches} == {events["unrelated_before"].id}


def test_similar_events_honours_an_explicit_before(corpus):
    events, service = corpus
    matches = service.similar_events(
        events["subject"],
        before=dt.datetime(2026, 5, 10, tzinfo=UTC),
        threshold=-1.0,
    )
    assert {m.event.id for m in matches} == {events["related_before"].id}


def test_similarity_scores_are_in_range(corpus):
    events, service = corpus
    for match in service.similar_events(events["subject"], threshold=-1.0):
        assert -1.0 <= match.similarity <= 1.0


# --- novelty --------------------------------------------------------------
def test_max_similarity_is_bounded_by_the_window(corpus):
    events, service = corpus
    # The related event is 31 days earlier, so a 7-day window must not see it.
    narrow, matched = service.max_similarity(events["subject"], window_days=7)
    assert narrow == 0.0 and matched is None

    wide, matched = service.max_similarity(events["subject"], window_days=60)
    assert wide > 0.0
    assert matched == events["related_before"].id


def test_novelty_uses_embeddings_when_available(corpus):
    from app.pipeline.historical import novelty

    events, service = corpus
    result = novelty(db_or_none := service.db, events["subject"], window_days=60, embeddings=service)
    assert db_or_none is not None
    assert result.max_similarity > 0.0
    assert result.value == pytest.approx(1.0 - result.max_similarity, abs=1e-4)
    assert "lexical n-grams" in result.measure


def test_novelty_falls_back_when_embeddings_fail(corpus):
    """A vector-search failure must cost the measure's quality, not the signal."""
    from app.pipeline.historical import novelty

    events, service = corpus

    class Broken:
        provider = service.provider

        def max_similarity(self, *args, **kwargs):
            raise RuntimeError("vector index unavailable")

    result = novelty(service.db, events["subject"], window_days=60, embeddings=Broken())
    assert 0.0 <= result.value <= 1.0
    assert "Jaccard" in result.measure, "the fallback must name itself"
