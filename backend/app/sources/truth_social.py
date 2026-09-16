"""Truth Social adapter -- **intentionally not implemented as a live scraper**.

There is no official public API, and the platform's terms of service prohibit
unauthorised automated access. Reachability is not permission. So this module
ships:

  * the adapter *interface*, so a licensed or permitted feed can be dropped in
    later without touching the pipeline;
  * a manual import path (``scripts/import_archive.py`` /
    ``POST /api/admin/import``) for CSV/JSON the operator obtains themselves;
  * an honest health status (``MANUAL_ONLY``) shown in the UI.

See docs/PHASE0_FEASIBILITY.md §1.1 for the full reasoning and for what the news
adapter does and does not cover in its place.
"""

from __future__ import annotations

import datetime as dt

from .base import RawItem, SourceAdapter


class TruthSocialUnavailable(RuntimeError):
    """Raised to make the gap explicit rather than silently returning nothing."""


class TruthSocialAdapter(SourceAdapter):
    key = "truth_social"
    name = "Truth Social (manual import only)"
    kind = "manual"
    priority = "high"

    #: Flipped only if a legitimate, terms-compliant feed is configured.
    live_feed_available = False

    def enabled(self) -> bool:
        return self.live_feed_available

    def fetch(self) -> list[RawItem]:
        raise TruthSocialUnavailable(
            "No terms-compliant public feed for Truth Social. Use the CSV/JSON "
            "importer (scripts/import_archive.py) to load posts you have obtained "
            "yourself."
        )


def item_from_import_row(row: dict) -> RawItem:
    """Normalise one row of an operator-supplied archive into a RawItem.

    Accepted keys: id/post_id, created_at/timestamp, text/content, url, media_url,
    author. Stores the whole row as the raw payload so nothing is lost.
    """
    raw_ts = row.get("created_at") or row.get("timestamp") or row.get("source_timestamp")
    if not raw_ts:
        raise ValueError("archive row is missing a timestamp")
    ts = dt.datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)

    text = row.get("text") or row.get("content") or ""
    if not text:
        raise ValueError("archive row is missing text")

    return RawItem(
        source_key="truth_social",
        external_id=str(row.get("id") or row.get("post_id") or f"import:{ts.isoformat()}"),
        title=row.get("title"),
        text=text,
        author=row.get("author") or "Donald J. Trump",
        url=row.get("url"),
        media_url=row.get("media_url"),
        source_timestamp=ts,
        payload=row,
    )
