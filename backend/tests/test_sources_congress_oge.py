"""Congress.gov and OGE adapters."""

from __future__ import annotations

import datetime as dt

import pytest

from app.sources.congress import CongressAdapter, _current_congress, _parse_date
from app.sources.oge import OGEAdapter, filing_to_item

UTC = dt.timezone.utc


# --- Congress -------------------------------------------------------------
def test_congress_is_disabled_without_a_key():
    adapter = CongressAdapter(api_key=None)
    assert adapter.enabled() is False
    with pytest.raises(RuntimeError, match="CONGRESS_API_KEY"):
        adapter.fetch()


def test_congress_is_enabled_with_a_key():
    assert CongressAdapter(api_key="k").enabled() is True


def test_congress_number_maths():
    # The 119th Congress convened in January 2025.
    assert _current_congress(dt.date(2025, 6, 1)) == 119
    assert _current_congress(dt.date(2026, 6, 1)) == 119
    assert _current_congress(dt.date(2027, 6, 1)) == 120


def test_congress_date_parsing():
    assert _parse_date("2026-03-04").tzinfo is not None
    assert _parse_date("2026-03-04T15:00:00Z").hour == 15
    assert _parse_date("not a date") is None


def bill(**overrides) -> dict:
    base = {
        "number": "1234",
        "title": "A bill to impose tariffs on imported semiconductors",
        "congress": 119,
        "originChamber": "House",
        "url": "https://api.congress.gov/v3/bill/119/hr/1234",
        "latestAction": {"actionDate": "2026-03-04", "text": "Passed House"},
        "updateDate": "2026-03-05",
    }
    base.update(overrides)
    return base


def test_bill_becomes_an_item_with_its_action():
    item = CongressAdapter(api_key="k")._to_item(bill(), "hr")
    assert item is not None
    assert item.title.startswith("HR 1234:")
    assert "Passed House" in item.text
    assert item.source_timestamp == dt.datetime(2026, 3, 4, tzinfo=UTC)
    assert item.payload["number"] == "1234"


def test_the_action_date_is_part_of_the_identity(db):
    """A bill that moves is a new event, not a duplicate of its introduction."""
    adapter = CongressAdapter(api_key="k")
    introduced = adapter._to_item(
        bill(latestAction={"actionDate": "2026-01-10", "text": "Introduced"}), "hr"
    )
    passed = adapter._to_item(
        bill(latestAction={"actionDate": "2026-03-04", "text": "Passed House"}), "hr"
    )
    assert introduced.external_id != passed.external_id
    assert introduced.content_hash != passed.content_hash


def test_a_bill_without_a_title_is_skipped():
    assert CongressAdapter(api_key="k")._to_item(bill(title=""), "hr") is None


# --- the keyword gate, before any model call ------------------------------
def test_an_irrelevant_bill_never_becomes_an_item():
    """The whole point: a post-office naming must not reach the pipeline, where
    failing the relevance gate would still cost a triage call."""
    adapter = CongressAdapter(api_key="k")
    row = bill(
        title="To designate the facility of the United States Postal Service at "
        "12 Main Street as the John Smith Post Office",
        latestAction={"actionDate": "2026-03-04", "text": "Referred to committee"},
    )
    assert adapter._to_item(row, "hr") is None
    assert adapter.filtered_out == 1


def test_a_market_relevant_bill_passes_the_gate():
    adapter = CongressAdapter(api_key="k")
    item = adapter._to_item(bill(), "hr")  # tariffs on semiconductors
    assert item is not None
    assert adapter.filtered_out == 0
    assert item.payload["relevance_score"] >= 0.5
    assert "tariff" in item.payload["relevance_reason"]


def test_the_congress_gate_is_stricter_than_the_pipeline_gate():
    """A bill that would squeak past the pipeline's 0.3 is rejected at 0.5."""
    from app.pipeline.relevance import score_relevance

    row = bill(title="A bill concerning border facility investment")
    text = f"{row['title']}. Latest action: {row['latestAction']['text']}"

    assert score_relevance(text, threshold=0.3).relevant is True
    assert CongressAdapter(api_key="k", relevance_threshold=0.5)._to_item(row, "hr") is None


def test_the_threshold_is_configurable():
    row = bill(title="A bill concerning border facility investment")
    assert CongressAdapter(api_key="k", relevance_threshold=0.2)._to_item(row, "hr") is not None


def test_the_filtered_counter_resets_between_fetches():
    adapter = CongressAdapter(api_key="k")
    adapter._to_item(bill(title="To name a post office"), "hr")
    assert adapter.filtered_out == 1
    try:
        adapter.fetch()
    except Exception:
        pass  # no network here; we only care that the counter was reset
    assert adapter.filtered_out == 0


def test_a_bill_without_any_date_is_skipped():
    row = bill(latestAction={}, updateDate=None)
    assert CongressAdapter(api_key="k")._to_item(row, "hr") is None


# --- OGE ------------------------------------------------------------------
def test_oge_is_disabled_without_a_configured_index():
    adapter = OGEAdapter(feed_url=None)
    assert adapter.enabled() is False
    with pytest.raises(RuntimeError, match="documents, not an API"):
        adapter.fetch()


def filing(**overrides) -> dict:
    base = {
        "filing_date": "2026-05-15",
        "individual": "A. Official",
        "filing_type": "OGE Form 278e Annual",
        "listed_entities": ["Apple Inc.", "Treasury bills"],
        "source_url": "https://example.invalid/filing/1",
        "document_reference": "DOC-1",
    }
    base.update(overrides)
    return base


def test_filing_normalises_into_an_item():
    item = filing_to_item(filing())
    assert item.source_key == "oge"
    assert item.external_id == "DOC-1"
    assert item.source_timestamp == dt.datetime(2026, 5, 15, tzinfo=UTC)
    assert item.payload["individual"] == "A. Official"
    assert item.payload["listed_entities"] == ["Apple Inc.", "Treasury bills"]


def test_filing_records_only_what_is_listed():
    """The hard rule: nothing about undisclosed holdings, and no value fields."""
    payload = filing_to_item(filing()).payload
    for forbidden in ("estimated_holdings", "implied_position", "value", "shares", "amount"):
        assert forbidden not in payload
    assert "Nothing here is inferred" in payload["disclaimer"]
    assert "undisclosed holdings are not represented" in payload["disclaimer"]
    assert "only what the filing itself lists" in filing_to_item(filing()).text


def test_filing_accepts_a_semicolon_separated_entity_string():
    item = filing_to_item(filing(listed_entities="Apple Inc.; Ford Motor ; "))
    assert item.payload["listed_entities"] == ["Apple Inc.", "Ford Motor"]


def test_filing_with_no_entities_is_still_valid():
    item = filing_to_item(filing(listed_entities=[]))
    assert item.payload["listed_entities"] == []
    assert "Entities listed" not in item.text


@pytest.mark.parametrize("missing", ["filing_date", "individual", "filing_type"])
def test_incomplete_filings_are_rejected(missing):
    row = filing()
    row[missing] = ""
    with pytest.raises(ValueError):
        filing_to_item(row)


def test_unparseable_filing_date_is_rejected():
    with pytest.raises(ValueError, match="unparseable"):
        filing_to_item(filing(filing_date="last Tuesday"))


def test_filing_accepts_us_style_dates():
    assert filing_to_item(filing(filing_date="05/15/2026")).source_timestamp.month == 5


def test_filing_falls_back_to_a_composite_identity():
    item = filing_to_item(filing(document_reference=""))
    assert item.external_id == "A. Official:OGE Form 278e Annual:2026-05-15"


# --- registry integration -------------------------------------------------
def test_unconfigured_api_sources_report_needs_key_not_error(db):
    from app.sources.registry import poll_source

    result = poll_source(db, CongressAdapter(api_key=None))
    db.commit()
    assert result["status"] == "NEEDS_KEY"


def test_unconfigured_manual_sources_report_manual_only(db):
    from app.sources.registry import poll_source

    result = poll_source(db, OGEAdapter(feed_url=None))
    db.commit()
    assert result["status"] == "MANUAL_ONLY"
