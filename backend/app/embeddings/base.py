"""Embedding provider interface.

Two implementations ship:

* ``hashing`` -- dependency-free and deterministic. It is a **lexical** embedding
  (hashed word and character n-grams), not a semantic one: it will call two
  differently-worded statements about the same policy dissimilar. It exists so
  the whole similarity pipeline runs, and is tested, with no model download and
  no torch.
* ``sentence-transformers`` -- a real local model. This is the production
  choice, and the one the spec asks for.

Which one produced a score is recorded on every row and shown in the UI, so a
lexical score is never mistaken for a semantic one.
"""

from __future__ import annotations

import math


class EmbeddingProvider:
    name = "base"
    #: True when the vectors carry meaning beyond word overlap.
    semantic = False
    #: Cosine similarity above which two events are "similar" FOR THIS PROVIDER.
    #: Scores are not comparable across providers -- a lexical embedder puts
    #: genuinely related texts around 0.3 where a transformer puts them at 0.7 --
    #: so the threshold travels with the provider rather than living in config.
    default_similarity_threshold = 0.5

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    @property
    def label(self) -> str:
        """Human-readable provenance, shown next to any similarity score."""
        raise NotImplementedError


def l2_normalise(vector: list[float]) -> list[float]:
    """Unit-length vectors make cosine similarity a plain dot product, which is
    what both pgvector's `<=>` and the Python fallback assume."""
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        # An all-zero vector has no direction. Returning it unchanged would make
        # every cosine comparison against it 0, which is the honest answer.
        return vector
    return [value / norm for value in vector]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity for vectors that may or may not be normalised."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))
