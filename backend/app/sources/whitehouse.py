"""WhiteHouse.gov adapter.

The site is a WordPress-style install whose feed paths have moved between
administrations, so this adapter takes a *list* of candidate feed URLs and uses
every one that parses. If none parse it raises, the source is marked DEGRADED,
and the Federal Register adapter (which is an official, documented, keyless API)
still covers executive orders and proclamations.
"""

from __future__ import annotations

import datetime as dt
import logging

import feedparser

from ..config import settings
from . import http
from .base import RawItem, SourceAdapter

log = logging.getLogger(__name__)


def parse_feed_datetime(entry: dict) -> dt.datetime:
    """Best-effort timestamp for a feed entry, always returned as aware UTC."""
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return dt.datetime(*parsed[:6], tzinfo=dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


def strip_html(value: str) -> str:
    import re

    text = re.sub(r"<[^>]+>", " ", value or "")
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#8217;", "'")
    return re.sub(r"\s+", " ", text).strip()


class WhiteHouseAdapter(SourceAdapter):
    key = "whitehouse"
    name = "WhiteHouse.gov"
    kind = "rss"
    priority = "high"

    def __init__(self, feed_urls: list[str] | None = None, max_items: int = 40) -> None:
        # `is None` rather than falsy: an explicit empty list means "no feeds".
        self.feed_urls = settings.whitehouse_feeds if feed_urls is None else feed_urls
        self.max_items = max_items

    def fetch(self) -> list[RawItem]:
        items: list[RawItem] = []
        errors: list[str] = []
        for url in self.feed_urls:
            try:
                response = http.get(url)
            except Exception as exc:  # per-feed isolation inside the adapter too
                errors.append(f"{url}: {exc}")
                continue
            parsed = feedparser.parse(response.text)
            if parsed.bozo and not parsed.entries:
                errors.append(f"{url}: not a parseable feed")
                continue
            for entry in parsed.entries[: self.max_items]:
                summary = strip_html(entry.get("summary") or entry.get("description") or "")
                title = strip_html(entry.get("title") or "")
                if not (title or summary):
                    continue
                items.append(
                    RawItem(
                        source_key=self.key,
                        external_id=entry.get("id") or entry.get("link") or f"{url}#{title}",
                        title=title,
                        text=summary or title,
                        author="The White House",
                        url=entry.get("link"),
                        source_timestamp=parse_feed_datetime(entry),
                        payload={"feed_url": url, "entry": {k: str(v) for k, v in entry.items()}},
                    )
                )
        if not items and errors:
            raise RuntimeError("; ".join(errors[:3]))
        if errors:
            log.warning("whitehouse: %d feed(s) failed: %s", len(errors), errors[0])
        return items
