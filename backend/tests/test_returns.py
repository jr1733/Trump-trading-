"""Return calculation, abnormal returns, and the sample-size flags."""

from __future__ import annotations

import datetime as dt

import pytest

from app.market.base import Bar, MarketDataError, MarketDataProvider
from app.market.calendar import EASTERN, session_open_utc
from app.market.mock_provider import MockMarketDataProvider
from app.market.service import MarketDataService
from app.models import Ticker
from app.pipeline.historical import percentile, sample_flag, summarise

UTC = dt.timezone.utc


class FixedProvider(MarketDataProvider):
    """Daily-only provider with hand-set closes, so returns are checkable by hand."""

    name = "fixed"

    def __init__(self, closes: dict[str, dict[dt.date, float]]) -> None:
        self.closes = closes

    def supports_intraday(self) -> bool:
        return False

    def fetch_daily(self, symbol: str, start: dt.date, end: dt.date) -> list[Bar]:
        rows = self.closes.get(symbol.upper(), {})
        return [
            Bar(
                symbol=symbol.upper(),
                interval="1d",
                ts=session_open_utc(day),
                open=price,
                high=price,
                low=price,
                close=price,
                adjusted_close=price,
                volume=1000.0,
            )
            for day, price in sorted(rows.items())
            if start <= day <= end
        ]


def series(base: float, pcts: list[float], start: dt.date) -> dict[dt.date, float]:
    """Closes over consecutive weekdays, each `pct` above the previous."""
    out: dict[dt.date, float] = {}
    day, price = start, base
    for pct in pcts:
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        price = price * (1 + pct)
        out[day] = round(price, 6)
        day += dt.timedelta(days=1)
    return out


@pytest.fixture
def fixed_market(db):
    # Mon 2026-03-02 .. Fri 2026-03-06, flat then a move on the reaction day.
    start = dt.date(2026, 3, 2)
    provider = FixedProvider(
        {
            # base day close 100, reaction day +5%, then +1%, +1%
            "AAPL": series(100.0, [0.0, 0.05, 0.01, 0.01, 0.0], start),
            # benchmark: flat, then +1% on the reaction day
            "SPY": series(500.0, [0.0, 0.01, 0.0, 0.0, 0.0], start),
            "XLK": series(200.0, [0.0, 0.02, 0.0, 0.0, 0.0], start),
        }
    )
    db.add(Ticker(symbol="AAPL", name="Apple Inc.", sector="Technology", sector_etf="XLK"))
    db.add(Ticker(symbol="SPY", name="SPDR S&P 500", asset_class="etf"))
    db.add(Ticker(symbol="XLK", name="Tech sector", asset_class="etf"))
    db.commit()
    return MarketDataService(db, provider=provider)


def test_daily_only_provider_reports_daily_horizons_only(fixed_market):
    assert fixed_market.available_horizons() == ["1d", "3d", "5d"]


def test_intraday_provider_reports_intraday_horizons(db):
    service = MarketDataService(db, provider=MockMarketDataProvider())
    assert "5m" in service.available_horizons()
    assert "1d" in service.available_horizons()


def test_one_day_return_measures_the_reaction_session(fixed_market):
    # Event during Tuesday's session; baseline is Monday's close.
    event = dt.datetime(2026, 3, 3, 11, 0, tzinfo=EASTERN)
    returns = fixed_market.compute_returns("AAPL", event, ["1d"])
    assert returns["1d"].basis == "intraday"
    assert returns["1d"].raw_return == pytest.approx(0.05, abs=1e-6)


def test_abnormal_return_subtracts_the_benchmark(fixed_market):
    event = dt.datetime(2026, 3, 3, 11, 0, tzinfo=EASTERN)
    result = fixed_market.compute_returns("AAPL", event, ["1d"])["1d"]
    assert result.benchmark_return == pytest.approx(0.01, abs=1e-6)
    assert result.abnormal_return == pytest.approx(0.04, abs=1e-6)


def test_sector_abnormal_return_uses_the_mapped_sector_etf(fixed_market):
    event = dt.datetime(2026, 3, 3, 11, 0, tzinfo=EASTERN)
    result = fixed_market.compute_returns("AAPL", event, ["1d"])["1d"]
    assert result.sector_return == pytest.approx(0.02, abs=1e-6)
    assert result.sector_abnormal_return == pytest.approx(0.03, abs=1e-6)


def test_overnight_event_is_labelled_next_open(fixed_market):
    event = dt.datetime(2026, 3, 3, 2, 0, tzinfo=EASTERN)  # 2 a.m.
    result = fixed_market.compute_returns("AAPL", event, ["1d"])["1d"]
    assert result.basis == "next_open"
    assert any("outside regular trading hours" in note for note in result.notes)
    # Same reaction session as the intraday case, from the same baseline.
    assert result.raw_return == pytest.approx(0.05, abs=1e-6)


def test_multi_day_horizon_compounds(fixed_market):
    event = dt.datetime(2026, 3, 3, 11, 0, tzinfo=EASTERN)
    result = fixed_market.compute_returns("AAPL", event, ["3d"])["3d"]
    # 1.05 * 1.01 * 1.01 - 1
    assert result.raw_return == pytest.approx(0.05 + 0.01 + 0.01 + 0.0006 + 0.0005, abs=2e-4)


def test_intraday_horizons_are_omitted_when_unsupported(fixed_market):
    event = dt.datetime(2026, 3, 3, 11, 0, tzinfo=EASTERN)
    returns = fixed_market.compute_returns("AAPL", event, ["5m", "1d"])
    assert "5m" not in returns, "a daily-only provider must not invent a 5-minute return"
    assert "1d" in returns


def test_missing_price_data_yields_no_return_rather_than_a_guess(db):
    service = MarketDataService(db, provider=FixedProvider({}))
    event = dt.datetime(2026, 3, 3, 11, 0, tzinfo=EASTERN)
    assert service.compute_returns("ZZZZ", event, ["1d"]) == {}


def test_provider_failure_degrades_to_cache(db):
    class Broken(FixedProvider):
        def fetch_daily(self, symbol, start, end):
            raise MarketDataError("provider down")

    service = MarketDataService(db, provider=Broken({}))
    # Must not raise -- the event still gets processed, just without statistics.
    assert service.ensure_daily("AAPL", dt.date(2026, 3, 2), dt.date(2026, 3, 6)) == []


# --- statistics -----------------------------------------------------------
def test_sample_flags_match_the_spec():
    assert sample_flag(0) == "unreliable"
    assert sample_flag(9) == "unreliable"
    assert sample_flag(10) == "limited"
    assert sample_flag(19) == "limited"
    assert sample_flag(20) == "ok"


def test_percentile_interpolates():
    values = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 0) == 0.0
    assert percentile(values, 50) == 2.0
    assert percentile(values, 100) == 4.0
    assert percentile(values, 25) == pytest.approx(1.0)


def test_summarise_computes_the_required_fields():
    raw = [0.01, -0.02, 0.03, 0.0, 0.05, -0.01, 0.02, 0.04, -0.03, 0.01, 0.02]
    abnormal = [r - 0.005 for r in raw]
    stats = summarise("1d", raw, abnormal, "daily")

    assert stats.n == 11
    assert stats.flag == "limited"
    assert stats.median == pytest.approx(0.01)
    assert stats.minimum == -0.03
    assert stats.maximum == 0.05
    # 7 positive, 3 negative, one exact zero -- a flat day counts as neither.
    assert stats.positive_pct == pytest.approx(100 * 7 / 11, abs=0.01)
    assert stats.negative_pct == pytest.approx(100 * 3 / 11, abs=0.01)
    assert stats.positive_pct + stats.negative_pct < 100.0
    assert stats.p5 is not None and stats.p95 is not None
    assert stats.median_abnormal == pytest.approx(0.005)


def test_summarise_on_empty_sample_is_unreliable_with_no_numbers():
    stats = summarise("1d", [], [], "daily")
    assert stats.n == 0
    assert stats.flag == "unreliable"
    assert stats.median is None and stats.mean is None


def test_mock_provider_is_deterministic():
    a = MockMarketDataProvider().fetch_daily("AAPL", dt.date(2026, 3, 2), dt.date(2026, 3, 6))
    b = MockMarketDataProvider().fetch_daily("AAPL", dt.date(2026, 3, 2), dt.date(2026, 3, 6))
    assert [bar.close for bar in a] == [bar.close for bar in b]
    assert all(bar.ts.tzinfo is not None for bar in a)
