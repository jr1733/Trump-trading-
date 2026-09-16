"""Federal Register adapter -- the authoritative, keyless fallback.

The Federal Register API is the official publication channel for executive
orders, proclamations, determinations and presidential memoranda. It is free,
documented and needs no API key. Its trade-off is *latency*: publication lags
the White House press release by hours to days, so this is a completeness
backstop rather than a fast source. Overlap with the WhiteHouse adapter is
removed by content-hash deduplication.
"""

from __future__ import annotations

import datetime as dt

from ..config import settings
from . import http
from .base import RawItem, SourceAdapter

PRESIDENTIAL_TYPES = ["executive_order", "proclamation", "presidential_memorandum", "determination"]


class FederalRegisterAdapter(SourceAdapter):
    key = "federal_register"
    name = "Federal Register (presidential documents)"
    kind = "api"
    priority = "slow"

    def __init__(self, lookback_days: int = 7, per_page: int = 40) -> None:
        self.lookback_days = lookback_days
        self.per_page = per_page

    def fetch(self) -> list[RawItem]:
        since = (dt.date.today() - dt.timedelta(days=self.lookback_days)).isoformat()
        params: list[tuple[str, str]] = [
            ("per_page", str(self.per_page)),
            ("order", "newest"),
            ("conditions[publication_date][gte]", since),
            ("fields[]", "document_number"),
            ("fields[]", "title"),
            ("fields[]", "abstract"),
            ("fields[]", "html_url"),
            ("fields[]", "publication_date"),
            ("fields[]", "signing_date"),
            ("fields[]", "presidential_document_type"),
            ("fields[]", "type"),
        ]
        for doc_type in PRESIDENTIAL_TYPES:
            params.append(("conditions[presidential_document_type][]", doc_type))

        response = http.get(settings.federal_register_base, params=params)
        body = response.json()

        items: list[RawItem] = []
        for row in body.get("results", []):
            # Prefer the signing date: that is when the market could have learned
            # of it. publication_date is when the Register printed it.
            raw_date = row.get("signing_date") or row.get("publication_date")
            if not raw_date:
                continue
            ts = dt.datetime.fromisoformat(raw_date).replace(tzinfo=dt.timezone.utc)
            title = row.get("title") or ""
            abstract = row.get("abstract") or ""
            items.append(
                RawItem(
                    source_key=self.key,
                    external_id=row.get("document_number") or row.get("html_url") or title,
                    title=title,
                    text=abstract or title,
                    author="Federal Register",
                    url=row.get("html_url"),
                    source_timestamp=ts,
                    payload=row,
                )
            )
        return items
