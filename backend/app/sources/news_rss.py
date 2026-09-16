"""News RSS adapter.

Storage policy: **headline, publication, author, timestamp, URL and a short
summary only.** Full article text is never stored -- see
docs/PHASE0_FEASIBILITY.md §1.5. The summary is truncated at `MAX_SUMMARY_CHARS`
on the way in, so the policy is enforced at ingestion rather than trusted.

Feeds are configurable (`NEWS_RSS_FEEDS`). One dead feed degrades that feed only.
"""

from __future__ import annotations

import logging

import feedparser

from ..config import settings
from . import http
from .base import RawItem, SourceAdapter
from .whitehouse import parse_feed_datetime, strip_html

log = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 400

# Only items mentioning the subject are ingested; a general markets feed would
# otherwise flood the pipeline with items no filter downstream can use.
SUBJECT_TERMS = ("trump", "white house", "president", "administration", "executive order")


class NewsRSSAdapter(SourceAdapter):
    key = "news_rss"
    name = "News RSS"
    kind = "rss"
    priority = "normal"

    def __init__(
        self,
        feed_urls: list[str] | None = None,
        max_items_per_feed: int = 25,
        require_subject: bool = True,
    ) -> None:
        # `is None` rather than falsy: an explicit empty list means "no feeds",
        # not "use the defaults".
        self.feed_urls = settings.news_rss_feeds if feed_urls is None else feed_urls
        self.max_items_per_feed = max_items_per_feed
        self.require_subject = require_subject

    def enabled(self) -> bool:
        return bool(self.feed_urls)

    def fetch(self) -> list[RawItem]:
        items: list[RawItem] = []
        errors: list[str] = []
        for url in self.feed_urls:
            try:
                response = http.get(url)
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                continue
            parsed = feedparser.parse(response.text)
            publication = strip_html(getattr(parsed.feed, "title", "") or url)
            if parsed.bozo and not parsed.entries:
                errors.append(f"{url}: not a parseable feed")
                continue
            for entry in parsed.entries[: self.max_items_per_feed]:
                title = strip_html(entry.get("title") or "")
                summary = strip_html(entry.get("summary") or entry.get("description") or "")
                if not title:
                    continue
                blob = f"{title} {summary}".lower()
                if self.require_subject and not any(t in blob for t in SUBJECT_TERMS):
                    continue
                items.append(
                    RawItem(
                        source_key=self.key,
                        external_id=entry.get("id") or entry.get("link") or f"{url}#{title}",
                        title=title,
                        text=summary[:MAX_SUMMARY_CHARS] or title,
                        author=strip_html(entry.get("author") or "") or None,
                        url=entry.get("link"),
                        source_timestamp=parse_feed_datetime(entry),
                        payload={
                            "publication": publication,
                            "feed_url": url,
                            # Deliberately NOT storing entry.content / full text.
                            "summary_truncated_at": MAX_SUMMARY_CHARS,
                        },
                    )
                )
        if not items and errors:
            raise RuntimeError("; ".join(errors[:3]))
        if errors:
            log.warning("news_rss: %d feed(s) failed: %s", len(errors), errors[0])
        return items
