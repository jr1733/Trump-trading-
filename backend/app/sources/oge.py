"""Office of Government Ethics adapter -- public filings only.

There is no bulk or JSON API for OGE filings. Public financial disclosures
(OGE Form 278e and similar) are published as documents, much of it scanned or
semi-structured PDF, through public portals. So this adapter works the way the
Truth Social one does: interface, manual import, and an honest status in the UI,
rather than a scraper pointed at a government site.

**The hard rule in this module: nothing is ever inferred.** We record what a
filing *lists* and where the document is. There is no field on this record that
could hold a holding nobody disclosed, and no code path that derives one. A
disclosure is a statement about a date in the past made under a threshold-based
reporting regime; treating it as a current portfolio would be wrong in several
directions at once.

If an operator has a structured index of filings they are entitled to use, set
`OGE_FEED_URL` to it and the adapter will read it. The expected shape is a JSON
array of the fields in `filing_to_item` below.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..config import settings
from . import http
from .base import RawItem, SourceAdapter

log = logging.getLogger(__name__)


class OGEAdapter(SourceAdapter):
    key = "oge"
    name = "OGE public filings (manual import or configured index)"
    kind = "manual"
    priority = "slow"

    def __init__(self, feed_url: str | None = None) -> None:
        self.feed_url = feed_url if feed_url is not None else settings.oge_feed_url

    def enabled(self) -> bool:
        return bool(self.feed_url)

    def fetch(self) -> list[RawItem]:
        if not self.enabled():
            raise RuntimeError(
                "No OGE index configured. Filings are documents, not an API: set "
                "OGE_FEED_URL to a structured index you are entitled to use, or "
                "import filings with scripts/import_archive.py."
            )
        payload = http.get(self.feed_url).json()
        rows = payload if isinstance(payload, list) else payload.get("filings", [])

        items: list[RawItem] = []
        for index, row in enumerate(rows):
            try:
                items.append(filing_to_item(row))
            except ValueError as exc:
                log.warning("skipping OGE row %d: %s", index, exc)
        return items


def filing_to_item(row: dict) -> RawItem:
    """Normalise one filing record.

    Required: `filing_date`, `individual`, `filing_type`. Optional: the entities
    or assets the filing lists, the source URL and a document reference.

    Note what is stored and what is not. `listed_entities` is what the filing
    says; there is deliberately no "estimated holdings", "implied position" or
    "value" field, because the moment such a field exists someone fills it in.
    """
    filing_date = row.get("filing_date") or row.get("date")
    individual = (row.get("individual") or row.get("name") or "").strip()
    filing_type = (row.get("filing_type") or row.get("type") or "").strip()

    if not filing_date:
        raise ValueError("filing is missing filing_date")
    if not individual:
        raise ValueError("filing is missing the individual it belongs to")
    if not filing_type:
        raise ValueError("filing is missing filing_type")

    timestamp = _parse_date(filing_date)
    if timestamp is None:
        raise ValueError(f"unparseable filing_date: {filing_date!r}")

    listed = row.get("listed_entities") or row.get("assets") or []
    if isinstance(listed, str):
        listed = [part.strip() for part in listed.split(";") if part.strip()]
    listed = [str(entry).strip() for entry in listed if str(entry).strip()]

    document_reference = (
        row.get("document_reference") or row.get("document_id") or row.get("reference") or ""
    )
    url = row.get("source_url") or row.get("url")

    entity_sentence = (
        f" Entities listed on the filing: {', '.join(listed[:40])}." if listed else ""
    )
    text = (
        f"{filing_type} filed by {individual} on {timestamp.date().isoformat()}."
        f"{entity_sentence} This record reflects only what the filing itself lists."
    )

    return RawItem(
        source_key="oge",
        external_id=str(
            document_reference or f"{individual}:{filing_type}:{timestamp.date().isoformat()}"
        ),
        title=f"{filing_type}: {individual}",
        text=text,
        author="Office of Government Ethics",
        url=url,
        source_timestamp=timestamp,
        payload={
            "filing_date": timestamp.date().isoformat(),
            "individual": individual,
            "filing_type": filing_type,
            "listed_entities": listed,
            "source_url": url,
            "document_reference": document_reference or None,
            # Stated explicitly so it survives into the stored payload and any
            # export made from it.
            "disclaimer": (
                "Lists only what the filing discloses. Nothing here is inferred, "
                "and undisclosed holdings are not represented."
            ),
        },
    )


def _parse_date(value: str) -> dt.datetime | None:
    raw = str(value).strip().replace("Z", "+00:00")
    for parser in (
        dt.datetime.fromisoformat,
        lambda v: dt.datetime.strptime(v, "%Y-%m-%d"),
        lambda v: dt.datetime.strptime(v, "%m/%d/%Y"),
    ):
        try:
            parsed = parser(raw)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    return None
