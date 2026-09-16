"""Mock source -- the default in local development and tests.

Reads `app/seed/mock_events.json` and returns items whose timestamps are
"recent" relative to now, so the Live feed has content on a fresh database with
no API keys and no network.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .base import RawItem, SourceAdapter

SEED_PATH = Path(__file__).resolve().parent.parent / "seed" / "mock_events.json"


def load_seed(path: Path | None = None) -> list[dict]:
    target = path or SEED_PATH
    if not target.exists():
        return []
    return json.loads(target.read_text())


class MockSourceAdapter(SourceAdapter):
    key = "mock"
    name = "Mock feed (local development)"
    kind = "mock"
    priority = "high"

    def __init__(self, path: Path | None = None, now: dt.datetime | None = None) -> None:
        self.path = path
        self._now = now

    def fetch(self) -> list[RawItem]:
        now = self._now or dt.datetime.now(dt.timezone.utc)
        items: list[RawItem] = []
        for row in load_seed(self.path):
            # `offset_minutes` is relative to "now" so the feed is always fresh.
            if "offset_minutes" in row:
                ts = now - dt.timedelta(minutes=int(row["offset_minutes"]))
            else:
                ts = dt.datetime.fromisoformat(row["source_timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.timezone.utc)
            items.append(
                RawItem(
                    source_key=self.key,
                    external_id=row["external_id"],
                    title=row.get("title"),
                    text=row["text"],
                    author=row.get("author", "Donald J. Trump"),
                    url=row.get("url"),
                    media_url=row.get("media_url"),
                    source_timestamp=ts,
                    payload=row,
                )
            )
        return items
