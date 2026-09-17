"""API surface: authentication, validation, and the main read paths."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.llm.fake import CannedAnthropicClient
from app.main import app
from app.market.service import MarketDataService
from app.models import Event, EventTicker
from app.pipeline.runner import run_pipeline
from app.seed import loader
from app.sources.base import RawItem
from app.sources.registry import store_raw_items

UTC = dt.timezone.utc
AUTH = {"Authorization": f"Bearer {settings.app_auth_token}"}


@pytest.fixture
def client(db):
    loader.seed_reference_data(db)
    loader.seed_user(db)
    text = (
        "We are looking very seriously at tariffs on consumer electronics. "
        "Apple Inc. has made commitments about building here."
    )
    store_raw_items(
        db,
        [
            RawItem(
                source_key="mock",
                external_id="api-1",
                title="Statement on consumer electronics tariffs",
                text=text,
                source_timestamp=dt.datetime(2026, 3, 4, 15, 30, tzinfo=UTC),
                payload={"text": text, "title": "Statement on consumer electronics tariffs"},
            )
        ],
    )
    db.commit()
    run_pipeline(db, client=CannedAnthropicClient(), market=MarketDataService(db))
    return TestClient(app)


# --- authentication -------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    ["/api/events", "/api/watchlist", "/api/notifications", "/api/alert-rules",
     "/api/preferences", "/api/push/status", "/api/dashboard", "/api/admin/data-quality"],
)
def test_user_endpoints_require_authentication(client, path):
    assert client.get(path).status_code == 401


def test_wrong_token_is_rejected(client):
    response = client.get("/api/events", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_malformed_authorization_header_is_rejected(client):
    assert client.get("/api/events", headers={"Authorization": "nope"}).status_code == 401
    assert client.get("/api/events", headers={"Authorization": "Bearer"}).status_code == 401


def test_public_config_needs_no_auth_and_leaks_no_secrets(client):
    body = client.get("/api/config").json()
    assert body["phase"] == 2
    blob = str(body).lower()
    for secret in ("anthropic_api_key", "sk-ant", "smtp_password", "web_push_private"):
        assert secret not in blob
    # The public VAPID key is the only key that may appear.
    assert "web_push_public_key" in body


def test_health_needs_no_auth(client):
    assert client.get("/api/health").status_code == 200


# --- reads ----------------------------------------------------------------
def test_events_list_shape(client):
    body = client.get("/api/events", headers=AUTH).json()
    assert body["total"] >= 1
    item = body["items"][0]
    assert {"id", "source_key", "excerpt", "tickers", "signals", "analysis"} <= set(item)
    # Always UTC on the wire; the browser converts to local time.
    assert item["source_timestamp"].endswith(("Z", "+00:00"))


def test_event_detail_and_404(client, db):
    event = db.execute(select(Event)).scalars().first()
    assert client.get(f"/api/events/{event.id}", headers=AUTH).status_code == 200
    assert client.get("/api/events/does-not-exist", headers=AUTH).status_code == 404


def test_ticker_detail_includes_horizons_and_provider(client):
    body = client.get("/api/tickers/AAPL", headers=AUTH).json()
    assert body["symbol"] == "AAPL"
    assert "1d" in body["available_horizons"]
    assert body["provider"] == "mock"
    assert isinstance(body["event_history"], list)


def test_signal_response_carries_the_why_panel(client):
    body = client.get("/api/events", headers=AUTH).json()
    signals = [s for item in body["items"] for s in item["signals"]]
    assert signals, "expected at least one signal"
    signal = signals[0]
    assert signal["uncertainties"], "the Why? panel is mandatory"
    assert signal["weights"]
    assert "components" in signal
    assert "historical_stats" in signal


def test_search_returns_hits_and_subsequent_returns(client):
    body = client.get("/api/search", params={"q": "tariffs"}, headers=AUTH).json()
    assert body["count"] >= 1
    assert "subsequent_returns" in body["items"][0]


def test_search_rejects_too_short_a_query(client):
    assert client.get("/api/search", params={"q": "a"}, headers=AUTH).status_code == 422


def test_dashboard_shape(client):
    body = client.get("/api/dashboard", headers=AUTH).json()
    assert {"top_signals", "latest_events", "market_context", "source_health",
            "notification_summary"} <= set(body)
    assert {c["symbol"] for c in body["market_context"]} == set(settings.market_context_symbols)


# --- writes and validation ------------------------------------------------
def test_watchlist_add_and_remove(client):
    added = client.post("/api/watchlist", json={"ticker": "msft"}, headers=AUTH).json()
    assert "MSFT" in {row["ticker"] for row in added["items"]}

    removed = client.delete("/api/watchlist/MSFT", headers=AUTH).json()
    assert "MSFT" not in {row["ticker"] for row in removed["items"]}


def test_watchlist_rejects_a_bogus_ticker(client):
    assert client.post("/api/watchlist", json={"ticker": "!!!"}, headers=AUTH).status_code == 422
    assert client.post("/api/watchlist", json={}, headers=AUTH).status_code == 422


def test_alert_rule_crud_and_validation(client):
    created = client.post(
        "/api/alert-rules",
        json={"name": "My rule", "rule_type": "new_event", "tickers": ["nvda"]},
        headers=AUTH,
    ).json()
    assert created["tickers"] == ["NVDA"]

    updated = client.put(
        f"/api/alert-rules/{created['id']}",
        json={"name": "Renamed", "rule_type": "new_event", "tickers": ["NVDA"]},
        headers=AUTH,
    ).json()
    assert updated["name"] == "Renamed"

    assert client.delete(f"/api/alert-rules/{created['id']}", headers=AUTH).status_code == 200
    assert client.delete(f"/api/alert-rules/{created['id']}", headers=AUTH).status_code == 404


def test_alert_rule_rejects_out_of_range_thresholds(client):
    response = client.post(
        "/api/alert-rules",
        json={"name": "bad", "rule_type": "signal_threshold", "bullish_threshold": 5},
        headers=AUTH,
    )
    assert response.status_code == 422


def test_preferences_round_trip_and_timezone_validation(client):
    ok = client.put(
        "/api/preferences",
        json={
            "enabled": True, "quiet_hours_start": 22, "quiet_hours_end": 7,
            "timezone": "Europe/London", "channels": ["in_app"], "min_confidence": 0.2,
            "min_sample_size": 5, "max_per_hour": 10, "digest_enabled": True,
            "digest_hour_local": 8,
        },
        headers=AUTH,
    )
    assert ok.status_code == 200
    assert ok.json()["timezone"] == "Europe/London"

    bad = client.put(
        "/api/preferences",
        json={"timezone": "Mars/Olympus_Mons"},
        headers=AUTH,
    )
    assert bad.status_code == 422


def test_push_subscribe_requires_both_keys(client):
    assert (
        client.post(
            "/api/push/subscribe",
            json={"endpoint": "https://push.example.invalid/x", "keys": {"p256dh": "a"}},
            headers=AUTH,
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/push/subscribe",
            json={
                "endpoint": "https://push.example.invalid/x",
                "keys": {"p256dh": "a", "auth": "b"},
            },
            headers=AUTH,
        ).status_code
        == 200
    )


def test_test_notification_lands_in_the_centre(client):
    assert client.post("/api/notifications/test", headers=AUTH).json()["created"] is True
    body = client.get("/api/notifications", headers=AUTH).json()
    assert any(n["title"] == "Test notification" for n in body["items"])


def test_mark_read_and_read_all(client):
    client.post("/api/notifications/test", headers=AUTH)
    listing = client.get("/api/notifications", headers=AUTH).json()
    first = listing["items"][0]

    client.post(f"/api/notifications/{first['id']}/read", headers=AUTH)
    assert client.get("/api/notifications", headers=AUTH).json()["unread_count"] < listing["unread_count"] or listing["unread_count"] == 1

    client.post("/api/notifications/read-all", headers=AUTH)
    assert client.get("/api/notifications", headers=AUTH).json()["unread_count"] == 0


def test_ticker_correction_is_stored_and_remembered(client, db):
    event = db.execute(select(Event)).scalars().first()
    response = client.post(
        f"/api/events/{event.id}/tickers",
        json={"ticker": "MSFT", "confidence": "HIGH", "remember_alias": "Redmond"},
        headers=AUTH,
    )
    assert response.status_code == 200
    assert "MSFT" in {t["ticker"] for t in response.json()["tickers"]}

    link = db.execute(
        select(EventTicker).where(EventTicker.event_id == event.id, EventTicker.ticker == "MSFT")
    ).scalars().one()
    assert link.user_corrected is True

    from app.models import EntityAlias

    alias = db.execute(
        select(EntityAlias).where(EntityAlias.alias == "Redmond")
    ).scalars().one()
    assert alias.ticker == "MSFT" and alias.user_corrected is True


def test_import_endpoint_validates_its_body(client):
    assert client.post("/api/admin/import", json={}, headers=AUTH).status_code == 422
    assert client.post("/api/admin/import", json={"rows": []}, headers=AUTH).status_code == 422

    ok = client.post(
        "/api/admin/import",
        json={
            "source": "truth_social",
            "rows": [
                {"id": "t1", "created_at": "2026-02-01T12:00:00Z", "text": "Tariffs are coming."}
            ],
        },
        headers=AUTH,
    )
    assert ok.status_code == 200
    assert ok.json()["inserted"] == 1


BANNED_PHRASES = ("buy now", "sell now", "guaranteed", "risk-free", "risk free")


def test_no_endpoint_emits_trading_language(client):
    """The UI must never say buy/sell/guaranteed/risk-free."""
    for path in [
        "/api/events",
        "/api/dashboard",
        "/api/digest",
        "/api/watchlist",
        "/api/digest/preview",
        "/api/backtest/runs",
        "/api/admin/data-quality",
    ]:
        blob = client.get(path, headers=AUTH).text.lower()
        for phrase in BANNED_PHRASES:
            assert phrase not in blob, f"{path} contains {phrase!r}"


@pytest.mark.parametrize("mode", ["rule_based", "llm"])
def test_backtest_notes_avoid_trading_language(mode):
    """The Sharpe note is the trap here: the textbook term for its numerator is
    one of the banned phrases, so it is deliberately spelled "cash rate"."""
    from app.pipeline import backtest as bt

    params = bt.BacktestParams(
        start=dt.date(2024, 1, 1), end=dt.date(2026, 1, 1), sentiment_mode=mode
    )
    result = bt.BacktestResult(params=params, overall={"n": 40, "sharpe": 0.4})
    bt._add_notes(result, params)

    blob = (" ".join(result.notes + result.warnings)).lower()
    assert "sharpe" in blob, "the note under test must actually be present"
    for phrase in BANNED_PHRASES:
        assert phrase not in blob, f"backtest notes contain {phrase!r}"


# --- Phase 2 endpoints ----------------------------------------------------
def test_similar_events_endpoint(client, db):
    event = db.execute(select(Event)).scalars().first()
    body = client.get(f"/api/events/{event.id}/similar", headers=AUTH).json()

    assert body["event_id"] == event.id
    assert body["provider"] == "hashing"
    assert body["semantic"] is False, "the UI must be able to say this is lexical"
    assert "measure" in body and body["measure"]
    assert isinstance(body["items"], list)


def test_similar_events_endpoint_404s_on_a_missing_event(client):
    assert client.get("/api/events/nope/similar", headers=AUTH).status_code == 404


def test_similar_events_requires_auth(client, db):
    event = db.execute(select(Event)).scalars().first()
    assert client.get(f"/api/events/{event.id}/similar").status_code == 401


def test_event_study_endpoint(client):
    body = client.get("/api/tickers/AAPL/event-study", headers=AUTH).json()
    assert body["ticker"] == "AAPL"
    assert body["benchmark"] == settings.benchmark_symbol
    assert "windows" in body and "skipped" in body
    assert body["sample_flag"] in {"ok", "limited", "unreliable"}
    assert body["notes"], "the method must be stated alongside the numbers"


def test_event_study_endpoint_requires_auth(client):
    assert client.get("/api/tickers/AAPL/event-study").status_code == 401


def test_embedding_status_endpoint(client):
    body = client.get("/api/admin/embeddings", headers=AUTH).json()
    assert body["provider"] == "hashing"
    assert body["dim"] == settings.embedding_dim
    assert body["events_total"] >= 1
    assert body["semantic"] is False


def test_embedding_backfill_endpoint_is_idempotent(client):
    first = client.post("/api/admin/embed", headers=AUTH).json()
    assert first["provider"] == "hashing"
    second = client.post("/api/admin/embed", headers=AUTH).json()
    assert second["embedded"] == 0
    assert second["remaining"] == 0


def test_push_retry_endpoint(client):
    body = client.post("/api/admin/push-retry", headers=AUTH).json()
    assert {"attempted", "sent", "failed", "expired", "subscriptions_pruned"} <= set(body)


def test_config_reports_the_embedding_provider(client):
    body = client.get("/api/config").json()
    assert body["embedding_provider"] == "hashing"
    assert body["embedding_semantic"] is False
    assert "not meaning" in body["embedding_label"]
