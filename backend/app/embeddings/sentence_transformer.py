"""Local sentence-transformers embedder.

This is the production choice. It is kept behind a lazy import and an optional
requirements file because `sentence-transformers` pulls in torch, which is
roughly 2 GB installed and would otherwise dominate the image on a small VPS.

    pip install -r requirements-embeddings.txt
    EMBEDDING_PROVIDER=sentence-transformers

Anthropic does not offer an embeddings API, so this genuinely does run locally:
the model is downloaded once (~90 MB for all-MiniLM-L6-v2) and then needs no
network. Model load is deferred to the first call so importing this module is
cheap and the worker starts immediately.

The model's output dimension must match `EMBEDDING_DIM` (384 for
all-MiniLM-L6-v2, which is the default). A mismatch is raised at load time
rather than silently written to a column the database will reject.
"""

from __future__ import annotations

import logging
import threading

from .base import EmbeddingProvider, l2_normalise

log = logging.getLogger(__name__)


class SentenceTransformerProvider(EmbeddingProvider):
    name = "sentence-transformers"
    semantic = True
    default_similarity_threshold = 0.5

    def __init__(self, model_name: str, dim: int) -> None:
        super().__init__(dim)
        self.model_name = model_name
        self._model = None
        # Model load is not thread-safe and the worker has several jobs.
        self._lock = threading.Lock()

    @property
    def label(self) -> str:
        return f"{self.model_name} ({self.dim}d) -- local semantic embeddings"

    def _load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - depends on install
                raise RuntimeError(
                    "EMBEDDING_PROVIDER=sentence-transformers but the package is not "
                    "installed. Run: pip install -r requirements-embeddings.txt"
                ) from exc

            log.info("loading embedding model %s (first call only)", self.model_name)
            model = SentenceTransformer(self.model_name)
            actual = int(model.get_sentence_embedding_dimension())
            if actual != self.dim:
                raise RuntimeError(
                    f"model {self.model_name} produces {actual}-dimensional vectors "
                    f"but EMBEDDING_DIM is {self.dim}. Set EMBEDDING_DIM={actual} and "
                    "re-run the embedding migration, or choose a matching model."
                )
            self._model = model
            return model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        vectors = model.encode(
            texts,
            batch_size=min(32, len(texts)),
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        # normalize_embeddings=True already unit-normalises; the second pass is
        # cheap insurance against a model or version that ignores the flag.
        return [l2_normalise([float(value) for value in row]) for row in vectors]
