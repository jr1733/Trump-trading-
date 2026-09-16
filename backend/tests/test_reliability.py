"""Retry, backoff, source isolation, and health tracking.

The property that matters: **one failing source never affects another**, and a
failure is recorded rather than raised.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from sqlalchemy import select

from app.models import RawEvent, SourceHealth
from app.sources.base import RawItem, SourceAdapter
from app.sources.http import RETRYABLE_STATUS, backoff_delay, with_retries
from app.sources.registry import poll_all, poll_source, record_failure, record_success

UTC = dt.timezone.utc


# --- backoff --------------------------------------------------------------
def test_backoff_grows_exponentially_without_jitter():
    assert backoff_delay(0, jitter=False) == 1.0
    assert backoff_delay(1, jitter=False) == 2.0
    assert backoff_delay(2, jitter=False) == 4.0


def test_backoff_is_capped():
    assert backoff_delay(20, jitter=False, cap=30.0) == 30.0


def test_backoff_jitter_stays_within_the_bound():
    values = [backoff_delay(3) for _ in range(50)]
    assert all(0 <= v <= 8.0 for v in values)
    assert len(set(values)) > 1, "jitter must actually vary"


# --- retries --------------------------------------------------------------
def test_retries_until_success():
    attempts = {"n": 0}
    slept: list[float] = []

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("boom")
        return "ok"

    assert with_retries(flaky, attempts=3, sleep=slept.append) == "ok"
    assert attempts["n"] == 3
    assert len(slept) == 2


def test_gives_up_after_the_attempt_budget():
    def always_fails():
        raise httpx.ConnectError("boom")

    with pytest.raises(httpx.ConnectError):
        with_retries(always_fails, attempts=2, sleep=lambda _: None)


def test_non_retryable_status_is_not_retried():
    calls = {"n": 0}

    def forbidden():
        calls["n"] += 1
        response = httpx.Response(403, request=httpx.Request("GET", "https://example.invalid"))
        raise httpx.HTTPStatusError("forbidden", request=response.request, response=response)

    with pytest.raises(httpx.HTTPStatusError):
        with_retries(forbidden, attempts=3, sleep=lambda _: None)
    assert calls["n"] == 1, "a 403 is not going to fix itself"


def test_retryable_statuses_include_rate_limiting():
    assert 429 in RETRYABLE_STATUS
    assert 503 in RETRYABLE_STATUS
    assert 404 not in RETRYABLE_STATUS


# --- source isolation -----------------------------------------------------
class GoodSource(SourceAdapter):
    key = "good"
    name = "Good"
    kind = "mock"

    def fetch(self) -> list[RawItem]:
        return [
            RawItem(
                source_key=self.key,
                external_id="g1",
                text="Tariffs on chips are under review.",
                source_timestamp=dt.datetime(2026, 3, 4, 15, 0, tzinfo=UTC),
                payload={"text": "Tariffs on chips are under review."},
            )
        ]


class BrokenSource(SourceAdapter):
    key = "broken"
    name = "Broken"
    kind = "rss"

    def fetch(self) -> list[RawItem]:
        raise httpx.ConnectError("upstream is down")


class ManualSource(SourceAdapter):
    key = "manual_only"
    name = "Manual"
    kind = "manual"

    def enabled(self) -> bool:
        return False

    def fetch(self) -> list[RawItem]:
        raise AssertionError("must never be called")


def test_one_broken_source_does_not_affect_the_others(db):
    results = poll_all(db, [BrokenSource(), GoodSource()])
    db.commit()

    by_source = {r["source"]: r for r in results}
    assert by_source["broken"]["status"] == "DEGRADED"
    assert by_source["good"]["status"] == "ONLINE"
    assert by_source["good"]["new_items"] == 1
    assert db.execute(select(RawEvent)).scalars().first() is not None


def test_poll_never_raises(db):
    # Explicitly: a source that raises must return a result dict, not propagate.
    result = poll_source(db, BrokenSource())
    db.commit()
    assert result["status"] == "DEGRADED"
    assert "upstream is down" in result["error"]


def test_repeated_failures_escalate_to_error(db):
    from app.config import settings

    for _ in range(settings.source_failure_alert_threshold):
        poll_source(db, BrokenSource())
    db.commit()

    health = db.execute(
        select(SourceHealth).where(SourceHealth.source_key == "broken")
    ).scalars().one()
    assert health.status == "ERROR"
    assert health.consecutive_failures >= settings.source_failure_alert_threshold


def test_success_clears_the_failure_counter(db):
    record_failure(db, "flappy", "boom")
    record_failure(db, "flappy", "boom")
    db.commit()
    health = record_success(db, "flappy", 3)
    db.commit()

    assert health.status == "ONLINE"
    assert health.consecutive_failures == 0
    assert health.last_error is None
    assert health.items_last_poll == 3


def test_manual_only_source_is_reported_not_polled(db):
    result = poll_source(db, ManualSource())
    db.commit()
    assert result["status"] == "MANUAL_ONLY"


def test_truth_social_adapter_refuses_to_scrape():
    from app.sources.truth_social import TruthSocialAdapter, TruthSocialUnavailable

    adapter = TruthSocialAdapter()
    assert adapter.enabled() is False
    with pytest.raises(TruthSocialUnavailable):
        adapter.fetch()


def test_truth_social_import_row_normalises():
    from app.sources.truth_social import item_from_import_row

    item = item_from_import_row(
        {"id": "123", "created_at": "2026-03-04T15:00:00Z", "text": "Tariffs are coming."}
    )
    assert item.source_key == "truth_social"
    assert item.source_timestamp.tzinfo is not None
    assert item.external_id == "123"


def test_truth_social_import_rejects_incomplete_rows():
    from app.sources.truth_social import item_from_import_row

    with pytest.raises(ValueError):
        item_from_import_row({"id": "1", "text": "no timestamp"})
    with pytest.raises(ValueError):
        item_from_import_row({"id": "1", "created_at": "2026-03-04T15:00:00Z"})


def test_raw_item_rejects_naive_timestamps():
    with pytest.raises(ValueError):
        RawItem(
            source_key="x",
            external_id="1",
            text="t",
            source_timestamp=dt.datetime(2026, 3, 4, 15, 0),
        )


def test_news_adapter_stores_only_a_short_summary():
    """Storage policy: headline + short summary, never full article text."""
    from app.sources.news_rss import MAX_SUMMARY_CHARS, NewsRSSAdapter

    adapter = NewsRSSAdapter(feed_urls=[])
    assert adapter.enabled() is False
    assert MAX_SUMMARY_CHARS <= 500
