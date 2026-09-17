"""Congress.gov adapter.

The official API (`api.congress.gov/v3`), which is free: a key comes from
api.data.gov and the documented limit is around 5,000 requests/hour. Without a
key the adapter disables itself cleanly rather than failing every poll.

We poll two things and treat them as separate events, because they are:

* **new bills** -- introduced since the last window;
* **status changes** -- the latest action on a bill, which is where the
  market-relevant information usually is. "Introduced" and "passed the House"
  are not the same event and must not deduplicate into each other, so the
  external id includes the action date.

Filtering by subject, sponsor and committee is available on the API, but a
keyword filter is applied here as well, because the bulk of congressional
activity has no market relevance and the point of the relevance gate is to keep
it out of the pipeline.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..config import settings
from . import http
from .base import RawItem, SourceAdapter

log = logging.getLogger(__name__)

API_BASE = "https://api.congress.gov/v3"

#: Bill types worth polling. Resolutions rarely carry economic consequence.
BILL_TYPES = ("hr", "s")


class CongressAdapter(SourceAdapter):
    key = "congress"
    name = "Congress.gov (bills and status changes)"
    kind = "api"
    priority = "slow"

    def __init__(
        self,
        api_key: str | None = None,
        lookback_days: int = 3,
        limit: int = 50,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.congress_api_key
        self.lookback_days = lookback_days
        self.limit = limit

    def enabled(self) -> bool:
        # No key is a configuration state, not a failure. Saying so lets the
        # UI show "needs a key" instead of a red ERROR nobody can act on.
        return bool(self.api_key)

    def fetch(self) -> list[RawItem]:
        if not self.enabled():
            raise RuntimeError("CONGRESS_API_KEY is not set")

        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self.lookback_days)
        items: list[RawItem] = []
        errors: list[str] = []

        for bill_type in BILL_TYPES:
            try:
                items.extend(self._fetch_bill_type(bill_type, since))
            except Exception as exc:
                # One bill type failing must not lose the other.
                errors.append(f"{bill_type}: {exc}")

        if not items and errors:
            raise RuntimeError("; ".join(errors[:3]))
        if errors:
            log.warning("congress: partial failure: %s", errors[0])
        return items

    def _fetch_bill_type(self, bill_type: str, since: dt.datetime) -> list[RawItem]:
        response = http.get(
            f"{API_BASE}/bill/{_current_congress()}/{bill_type}",
            params={
                "api_key": self.api_key,
                "format": "json",
                "limit": str(self.limit),
                "sort": "updateDate+desc",
                "fromDateTime": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        payload = response.json()

        items: list[RawItem] = []
        for bill in payload.get("bills", []):
            item = self._to_item(bill, bill_type)
            if item is not None:
                items.append(item)
        return items

    def _to_item(self, bill: dict, bill_type: str) -> RawItem | None:
        number = bill.get("number")
        title = (bill.get("title") or "").strip()
        if not number or not title:
            return None

        action = bill.get("latestAction") or {}
        action_text = (action.get("text") or "").strip()
        action_date = action.get("actionDate") or bill.get("updateDate")
        if not action_date:
            return None

        timestamp = _parse_date(action_date)
        if timestamp is None:
            return None

        label = f"{bill_type.upper()} {number}"
        text = f"{title}. Latest action: {action_text}" if action_text else title

        return RawItem(
            source_key=self.key,
            # The action date is part of the id on purpose: a bill that moves
            # is a new event, not a duplicate of its own introduction.
            external_id=f"{label}:{action_date}",
            title=f"{label}: {title[:180]}",
            text=text,
            author="Congress.gov",
            url=bill.get("url"),
            source_timestamp=timestamp,
            payload={
                "bill_type": bill_type,
                "number": number,
                "congress": bill.get("congress"),
                "latest_action": action,
                "origin_chamber": bill.get("originChamber"),
                "raw": bill,
            },
        )


def _current_congress(today: dt.date | None = None) -> int:
    """Congress number for a date.

    The 1st Congress convened in 1789 and each runs two years, starting in odd
    years: `1789 + 2*(n-1)`, inverted.
    """
    year = (today or dt.date.today()).year
    return (year - 1789) // 2 + 1


def _parse_date(value: str) -> dt.datetime | None:
    raw = str(value).strip().replace("Z", "+00:00")
    for parser in (dt.datetime.fromisoformat, lambda v: dt.datetime.strptime(v, "%Y-%m-%d")):
        try:
            parsed = parser(raw)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    return None
