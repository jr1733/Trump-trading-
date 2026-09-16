"""Embedding storage and similarity search.

Vectors live in pgvector and are searched with the cosine distance operator
`<=>`, so the database does the work and the HNSW index is actually used.
Because every vector is L2-normalised, cosine distance is `1 - similarity`.

**Look-ahead prevention applies here too.** `similar_events` takes a `before`
cut-off and enforces it in SQL, for the same reason `find_comparable_events`
does: a similarity search that can see the future silently invalidates every
statistic built on top of it.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Event, EventEmbedding, EventTicker, utcnow
from .base import EmbeddingProvider
from .hashing import HashingEmbeddingProvider

log = logging.getLogger(__name__)

_PROVIDER_CACHE: dict[tuple[str, str, int], EmbeddingProvider] = {}


def vector_literal(vector: list[float]) -> str:
    """pgvector's text input format.

    Built explicitly rather than with `str(list)`: a vector read back from the
    database arrives as numpy float32 values whose repr is `np.float32(0.1)`,
    which pgvector rejects. Coercing through `float()` here means it does not
    matter where the vector came from.
    """
    return "[" + ",".join(f"{float(value):.9g}" for value in vector) + "]"


def build_provider(name: str | None = None) -> EmbeddingProvider:
    """Providers are cached: loading a transformer model twice is expensive."""
    key = (
        (name or settings.embedding_provider).lower(),
        settings.embedding_model,
        settings.embedding_dim,
    )
    cached = _PROVIDER_CACHE.get(key)
    if cached is not None:
        return cached

    provider_name = key[0]
    if provider_name in ("sentence-transformers", "sentence_transformers", "st"):
        from .sentence_transformer import SentenceTransformerProvider

        provider: EmbeddingProvider = SentenceTransformerProvider(
            settings.embedding_model, settings.embedding_dim
        )
    else:
        if provider_name != "hashing":
            log.warning("unknown embedding provider %r; falling back to hashing", provider_name)
        provider = HashingEmbeddingProvider(settings.embedding_dim)

    _PROVIDER_CACHE[key] = provider
    return provider


def embedding_text(event: Event) -> str:
    """What actually gets embedded. Title first: it is the densest summary."""
    return f"{event.title or ''}\n{event.text or ''}".strip()


@dataclass
class SimilarEvent:
    event: Event
    similarity: float

    def as_dict(self) -> dict:
        return {
            "event_id": self.event.id,
            "title": self.event.title or (self.event.text[:120] if self.event.text else ""),
            "source": self.event.source_key,
            "event_type": self.event.event_type,
            "source_timestamp": self.event.source_timestamp.isoformat(),
            "similarity": round(self.similarity, 4),
            "is_historical": self.event.is_historical,
        }


class EmbeddingService:
    def __init__(self, db: Session, provider: EmbeddingProvider | None = None) -> None:
        self.db = db
        self.provider = provider or build_provider()

    @property
    def model_name(self) -> str:
        return getattr(self.provider, "model_name", self.provider.name)

    @property
    def threshold(self) -> float:
        """The provider's own threshold, unless config overrides it.

        `SIMILARITY_THRESHOLD` is left unset by default precisely so the
        provider-appropriate value applies: a number tuned for a transformer
        would reject every genuine match from the lexical embedder.
        """
        override = settings.similarity_threshold
        if override is None:
            return self.provider.default_similarity_threshold
        return override

    # -- writing ----------------------------------------------------------
    def store(self, event: Event, vector: list[float]) -> EventEmbedding:
        """Upsert one vector. Idempotent: re-embedding replaces in place."""
        values = {
            "event_id": event.id,
            "provider": self.provider.name,
            "model": self.model_name[:96],
            "dim": self.provider.dim,
            "embedding": vector,
            "content_hash": event.content_hash,
            "created_at": utcnow(),
        }
        stmt = insert(EventEmbedding).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[EventEmbedding.event_id],
            set_={
                "provider": stmt.excluded.provider,
                "model": stmt.excluded.model,
                "dim": stmt.excluded.dim,
                "embedding": stmt.excluded.embedding,
                "content_hash": stmt.excluded.content_hash,
                "created_at": stmt.excluded.created_at,
            },
        )
        self.db.execute(stmt)
        self.db.flush()
        return self.db.get(EventEmbedding, event.id)

    def embed_event(self, event: Event) -> EventEmbedding:
        return self.store(event, self.provider.embed_one(embedding_text(event)))

    def pending_events(self, limit: int = 200) -> list[Event]:
        """Events with no vector, or whose vector is stale.

        Stale means: a different provider (a vector from another embedding space
        is not comparable), or a content hash that no longer matches the event.
        """
        stmt = (
            select(Event)
            .outerjoin(EventEmbedding, EventEmbedding.event_id == Event.id)
            .where(
                (EventEmbedding.event_id.is_(None))
                | (EventEmbedding.provider != self.provider.name)
                | (EventEmbedding.content_hash != Event.content_hash)
            )
            .order_by(Event.source_timestamp.desc())
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars().unique())

    def backfill(self, limit: int = 200) -> int:
        """Embed a batch of pending events. Returns how many were written."""
        events = self.pending_events(limit)
        if not events:
            return 0
        written = 0
        size = max(1, settings.embedding_batch_size)
        for start in range(0, len(events), size):
            batch = events[start : start + size]
            try:
                vectors = self.provider.embed([embedding_text(e) for e in batch])
            except Exception as exc:
                # A model that will not load must not wedge the pipeline; the
                # rest of the app works without embeddings.
                log.error("embedding batch failed: %s", exc)
                break
            for event, vector in zip(batch, vectors):
                self.store(event, vector)
                written += 1
        return written

    # -- reading ----------------------------------------------------------
    def vector_for(self, event: Event) -> list[float] | None:
        row = self.db.get(EventEmbedding, event.id)
        if row is None or row.provider != self.provider.name:
            return None
        return [float(value) for value in row.embedding]

    def similar_events(
        self,
        event: Event,
        *,
        before: dt.datetime | None = None,
        since: dt.datetime | None = None,
        limit: int | None = None,
        threshold: float | None = None,
        ticker: str | None = None,
        event_type: str | None = None,
        include_historical: bool = True,
    ) -> list[SimilarEvent]:
        """Nearest neighbours by cosine similarity, strictly before `before`.

        `before` defaults to the subject event's own timestamp, which is the
        safe default: an unqualified similarity search that reaches forward in
        time is the classic way to leak the future into a backtest.
        """
        vector = self.vector_for(event)
        if vector is None:
            vector = self.provider.embed_one(embedding_text(event))

        cutoff = before if before is not None else event.source_timestamp
        limit = limit or settings.similar_events_limit
        threshold = self.threshold if threshold is None else threshold
        max_distance = 1.0 - threshold

        # Raw SQL for the distance operator: SQLAlchemy has no portable spelling
        # for `<=>`, and we want the ORDER BY to hit the HNSW index.
        params: dict = {
            "vector": vector_literal(vector),
            "event_id": event.id,
            "cutoff": cutoff,
            "max_distance": max_distance,
            "limit": limit,
            "provider": self.provider.name,
        }
        conditions = [
            "ee.provider = :provider",
            "e.id <> :event_id",
            "e.source_timestamp < :cutoff",
        ]
        if since is not None:
            # Bounding the window in SQL, not after ranking: filtering the
            # top-1 result by date would report "no similar event" whenever the
            # single nearest neighbour happened to fall outside the window.
            conditions.append("e.source_timestamp >= :since")
            params["since"] = since
        if not include_historical:
            conditions.append("e.is_historical = false")
        if event_type:
            conditions.append("e.event_type = :event_type")
            params["event_type"] = event_type
        if ticker:
            conditions.append(
                "EXISTS (SELECT 1 FROM event_tickers et WHERE et.event_id = e.id "
                "AND et.ticker = :ticker AND et.confidence IN ('HIGH','MEDIUM'))"
            )
            params["ticker"] = ticker.upper()

        sql = text(
            f"""
            SELECT e.id AS event_id, (ee.embedding <=> CAST(:vector AS vector)) AS distance
            FROM event_embeddings ee
            JOIN events e ON e.id = ee.event_id
            WHERE {' AND '.join(conditions)}
              AND (ee.embedding <=> CAST(:vector AS vector)) <= :max_distance
            ORDER BY ee.embedding <=> CAST(:vector AS vector)
            LIMIT :limit
            """
        )
        rows = self.db.execute(sql, params).all()
        if not rows:
            return []

        events = {
            e.id: e
            for e in self.db.execute(
                select(Event).where(Event.id.in_([r.event_id for r in rows]))
            ).scalars().unique()
        }
        results = [
            SimilarEvent(event=events[row.event_id], similarity=1.0 - float(row.distance))
            for row in rows
            if row.event_id in events
        ]
        return results

    def max_similarity(
        self, event: Event, *, window_days: int | None = None
    ) -> tuple[float, str | None]:
        """Highest similarity to any event in the trailing window, for novelty.

        Returns `(similarity, matched_event_id)`; `(0.0, None)` when there is
        nothing to compare against.
        """
        window = window_days or settings.novelty_window_days
        matches = self.similar_events(
            event,
            before=event.source_timestamp,
            since=event.source_timestamp - dt.timedelta(days=window),
            limit=1,
            threshold=-1.0,  # we want the maximum in the window, whatever it is
        )
        if not matches:
            return 0.0, None
        best = matches[0]
        return max(0.0, best.similarity), best.event.id


def ticker_of(db: Session, event_id: str) -> str | None:
    row = (
        db.execute(
            select(EventTicker.ticker)
            .where(
                EventTicker.event_id == event_id,
                EventTicker.confidence.in_(["HIGH", "MEDIUM"]),
            )
            .limit(1)
        )
        .scalars()
        .first()
    )
    return row
