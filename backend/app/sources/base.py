"""Source adapter interface.

Every adapter implements ``fetch() -> list[RawItem]`` and is run in isolation:
the poller catches everything an adapter can raise, records it against that
source's health row, and moves on to the next source. One broken feed never
affects another.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

_WHITESPACE = re.compile(r"\s+")


def content_hash(*parts: str | None) -> str:
    """Stable hash used for deduplication and the LLM response cache.

    Normalised so that trivial reformatting (extra whitespace, case) does not
    produce a "new" event -- that is the difference between deduplicating a
    re-published press release and analysing it twice.
    """
    joined = "\n".join(_WHITESPACE.sub(" ", (p or "")).strip().lower() for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass
class RawItem:
    """One item as returned by a source, before normalisation."""

    source_key: str
    external_id: str
    text: str
    source_timestamp: dt.datetime
    title: str | None = None
    author: str | None = None
    url: str | None = None
    media_url: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source_timestamp.tzinfo is None:
            raise ValueError("RawItem.source_timestamp must be timezone-aware (UTC)")
        self.source_timestamp = self.source_timestamp.astimezone(dt.timezone.utc)

    @property
    def content_hash(self) -> str:
        return content_hash(self.title, self.text, self.url)


class SourceAdapter:
    """Base class. Subclasses set `key`, `name`, `kind` and implement `fetch`."""

    key = "base"
    name = "Base"
    kind = "api"
    priority = "normal"

    def enabled(self) -> bool:
        return True

    def fetch(self) -> list[RawItem]:
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} key={self.key}>"
