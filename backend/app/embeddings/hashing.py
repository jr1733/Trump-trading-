"""Dependency-free hashing embedder.

The hashing trick: project word unigrams, word bigrams and character 4-grams
into a fixed number of buckets with a stable hash, weight them sub-linearly,
then L2-normalise. No vocabulary to fit, no model to download, and identical
output on every machine.

**This is lexical, not semantic.** "Tariffs on chips" and "semiconductor import
duties" are near-orthogonal to it. That limitation is the reason the provider
name is stored alongside every similarity score and rendered in the UI.
"""

from __future__ import annotations

import hashlib
import math
import re

from .base import EmbeddingProvider, l2_normalise

_TOKEN = re.compile(r"[a-z0-9']+")

# Very common words carry no discriminating signal and would dominate the
# overlap between any two political statements.
STOPWORDS = frozenset(
    """a an and are as at be been but by for from had has have he her his i if in
    is it its of on or our that the their there they this to was we were will
    with you your""".split()
)


def _bucket(token: str, dim: int) -> tuple[int, float]:
    """Stable bucket index plus a sign, so unrelated tokens can cancel."""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % dim, 1.0 if (value >> 63) & 1 else -1.0


class HashingEmbeddingProvider(EmbeddingProvider):
    name = "hashing"
    semantic = False
    # Lexical overlap tops out much lower than semantic similarity: on the
    # sample archive, genuinely comparable statements score ~0.30-0.45.
    default_similarity_threshold = 0.25

    def __init__(self, dim: int = 384, char_ngram: int = 4) -> None:
        super().__init__(dim)
        self.char_ngram = char_ngram

    @property
    def label(self) -> str:
        return f"hashed lexical n-grams ({self.dim}d) -- word overlap, not meaning"

    # Each feature family is normalised separately before being combined, so a
    # long document's thousands of character n-grams cannot drown out its
    # handful of distinctive words. Weights sum to 1.
    FAMILY_WEIGHTS = {"word": 0.55, "bigram": 0.30, "char": 0.15}

    def _families(self, text: str) -> dict[str, dict[str, float]]:
        lowered = (text or "").lower()
        words = [w for w in _TOKEN.findall(lowered) if w not in STOPWORDS and len(w) > 1]

        families: dict[str, dict[str, float]] = {"word": {}, "bigram": {}, "char": {}}
        for word in words:
            families["word"][f"w:{word}"] = families["word"].get(f"w:{word}", 0.0) + 1.0
        for i in range(len(words) - 1):
            key = f"b:{words[i]}_{words[i + 1]}"
            families["bigram"][key] = families["bigram"].get(key, 0.0) + 1.0
        # Character n-grams give partial credit for shared morphology
        # ("tariff"/"tariffs") that word features alone would miss.
        squashed = " ".join(words)
        n = self.char_ngram
        for i in range(max(0, len(squashed) - n + 1)):
            key = f"c:{squashed[i : i + n]}"
            families["char"][key] = families["char"].get(key, 0.0) + 1.0
        return families

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            combined = [0.0] * self.dim
            for family, counts in self._families(text).items():
                if not counts:
                    continue
                partial = [0.0] * self.dim
                for feature, count in counts.items():
                    index, sign = _bucket(feature, self.dim)
                    # Sub-linear weighting: the tenth "tariff" says much less
                    # than the first, exactly as in TF-IDF.
                    partial[index] += sign * (1.0 + math.log(count))
                partial = l2_normalise(partial)
                weight = self.FAMILY_WEIGHTS[family]
                for i, value in enumerate(partial):
                    combined[i] += weight * value
            vectors.append(l2_normalise(combined))
        return vectors
