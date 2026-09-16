"""Event studies: OLS market model, abnormal returns, CARs, look-ahead."""

from __future__ import annotations

import datetime as dt

import pytest

from app.config import settings
from app.market.base import Bar, MarketDataProvider
from app.market.calendar import session_open_utc
from app.market.service import MarketDataService
from app.models import Event, EventTicker, Ticker
from app.pipeline.event_study import (
    DEFAULT_WINDOWS,
    car_t_statistic,
    cumulative,
    estimate_market_model,
    abnormal_returns,
    ordinary_least_squares,
    run_event_study,
    trading_days_between,
    window_label,
)

UTC = dt.timezone.utc


# --- OLS ------------------------------------------------------------------
def test_ols_recovers_a_known_line():
    xs = [0.01, -0.02, 0.03, 0.0, 0.015, -0.01, 0.02, 0.005]
    ys = [0.002 + 1.5 * x for x in xs]
    model = ordinary_least_squares(xs, ys)

    assert model.beta == pytest.approx(1.5, abs=1e-9)
    assert model.alpha == pytest.approx(0.002, abs=1e-9)
    assert model.residual_sd == pytest.approx(0.0, abs=1e-9)
    assert model.r_squared == pytest.approx(1.0, abs=1e-9)


def test_ols_reports_residual_spread_on_noisy_data():
    xs = [0.01, -0.02, 0.03, 0.0, 0.015, -0.01, 0.02, 0.005]
    ys = [0.5 * x + noise for x, noise in zip(xs, [0.01, -0.01, 0.02, -0.02, 0.01, -0.01, 0.0, 0.01])]
    model = ordinary_least_squares(xs, ys)
    assert model.residual_sd > 0
    assert model.r_squared < 1.0


def test_ols_refuses_a_constant_benchmark():
    """A benchmark that never moves cannot identify beta."""
    assert ordinary_least_squares([0.0] * 10, [0.01] * 10) is None


def test_ols_refuses_too_few_points():
    assert ordinary_least_squares([0.01, 0.02], [0.01, 0.02]) is None


def test_expected_return_uses_alpha_and_beta():
    model = ordinary_least_squares([0.01, -0.01, 0.02, 0.0], [0.02, -0.02, 0.04, 0.0])
    assert model.expected(0.01) == pytest.approx(0.02, abs=1e-9)


# --- windows --------------------------------------------------------------
def test_window_labels_are_readable():
    assert window_label((0, 0)) == "[+0,+0]"
    assert window_label((-1, 1)) == "[-1,+1]"


def test_cumulative_sums_a_complete_window():
    assert cumulative({0: 0.01, 1: 0.02, 2: -0.005}, (0, 2)) == pytest.approx(0.025)


def test_cumulative_refuses_an_incomplete_window():
    """A partial CAR reported as a full one would understate the move."""
    assert cumulative({0: 0.01, 2: 0.02}, (0, 2)) is None


def test_t_statistic_scales_with_the_window():
    model = ordinary_least_squares([0.01, -0.01, 0.02, 0.0, 0.03], [0.02, -0.03, 0.04, 0.0, 0.05])
    one_day = car_t_statistic(0.05, model, 1)
    five_day = car_t_statistic(0.05, model, 5)
    assert abs(one_day) > abs(five_day), "a longer window has a wider null distribution"


def test_t_statistic_is_none_without_residual_spread():
    from app.pipeline.event_study import MarketModel

    assert car_t_statistic(0.05, MarketModel(0, 1, 0.0, 100, 1.0), 1) is None


def test_trading_days_between_skips_weekends():
    days = trading_days_between(dt.date(2026, 3, 2), dt.date(2026, 3, 8))
    assert len(days) == 5
    assert all(d.weekday() < 5 for d in days)


# --- a controlled market -------------------------------------------------
class ScriptedProvider(MarketDataProvider):
    """Benchmark is a fixed walk; the stock is beta*market + a chosen shock.

    Prices always accumulate from a fixed epoch, never from the start of the
    requested range. Otherwise two fetches covering the same day would disagree
    about its price, and the cached series would imply returns nobody scripted.
    """

    name = "scripted"
    EPOCH = dt.date(2024, 1, 2)

    def __init__(self, beta: float = 1.0, shock: dict[dt.date, float] | None = None) -> None:
        self.beta = beta
        self.shock = shock or {}

    def supports_intraday(self) -> bool:
        return False

    def _market_return(self, day: dt.date) -> float:
        # Deterministic, roughly mean-zero, and definitely not constant.
        return 0.01 if (day.toordinal() % 2) else -0.008

    def fetch_daily(self, symbol: str, start: dt.date, end: dt.date) -> list[Bar]:
        if end < self.EPOCH:
            return []  # no history before the epoch: the study must skip it

        is_benchmark = symbol.upper() == settings.benchmark_symbol.upper()
        bars: list[Bar] = []
        price = 100.0
        for day in trading_days_between(self.EPOCH, end):
            market_return = self._market_return(day)
            change = (
                market_return
                if is_benchmark
                else self.beta * market_return + self.shock.get(day, 0.0)
            )
            price *= 1.0 + change
            if day < start:
                continue
            bars.append(
                Bar(
                    symbol=symbol.upper(),
                    interval="1d",
                    ts=session_open_utc(day),
                    open=price,
                    high=price,
                    low=price,
                    close=round(price, 8),
                    adjusted_close=round(price, 8),
                    volume=1000.0,
                )
            )
        return bars


@pytest.fixture
def scripted(db):
    db.add(Ticker(symbol="AAPL", name="Apple Inc.", sector="Technology", sector_etf="XLK"))
    db.add(Ticker(symbol="SPY", name="S&P 500", asset_class="etf"))
    db.commit()
    return db


def add_event(db, when: dt.datetime, *, ticker="AAPL", event_type="tariff") -> Event:
    import hashlib

    event = Event(
        source_key="mock",
        external_id=f"es-{when.isoformat()}",
        title="Tariff statement",
        text="Tariffs on consumer electronics are under review.",
        content_hash=hashlib.sha256(when.isoformat().encode()).hexdigest(),
        event_type=event_type,
        source_timestamp=when,
        relevant=True,
    )
    db.add(event)
    db.flush()
    db.add(EventTicker(event_id=event.id, ticker=ticker, confidence="HIGH"))
    db.commit()
    db.refresh(event)
    return event


def test_market_model_recovers_beta(scripted):
    market = MarketDataService(scripted, provider=ScriptedProvider(beta=1.8))
    model, reason = estimate_market_model(market, "AAPL", dt.date(2026, 6, 1))
    assert reason is None
    assert model.beta == pytest.approx(1.8, abs=0.05)
    assert model.observations >= settings.event_study_min_observations


def test_market_model_is_refused_without_enough_history(scripted):
    market = MarketDataService(scripted, provider=ScriptedProvider())
    model, reason = estimate_market_model(
        market, "AAPL", dt.date(2026, 6, 1), estimation_days=10
    )
    assert model is None
    assert "overlapping return observations" in reason


def test_abnormal_return_isolates_the_shock(scripted):
    """With a known beta and a known event-day shock, AR must equal the shock."""
    event_day = dt.date(2026, 6, 1)
    market = MarketDataService(
        scripted, provider=ScriptedProvider(beta=1.2, shock={event_day: 0.04})
    )
    model, _ = estimate_market_model(market, "AAPL", event_day)
    ars = abnormal_returns(market, "AAPL", event_day, model)

    assert ars[0] == pytest.approx(0.04, abs=0.003)
    # Neighbouring days had no shock, so their abnormal return is ~zero.
    assert abs(ars[1]) < 0.005


def first_sessions(months: list[tuple[int, int]]) -> list[dt.date]:
    """First trading day of each (year, month).

    Chosen rather than a fixed day-of-month: a shock scripted onto a Saturday
    or a market holiday never enters the price series at all, which would make
    the test measure the calendar rather than the code.
    """
    from app.market.calendar import next_trading_day

    return [next_trading_day(dt.date(y, m, 1), inclusive=True) for y, m in months]


def test_event_study_detects_a_consistent_shock(scripted):
    """Ten events each with a +3% shock should produce a positive, significant CAAR."""
    days = first_sessions([(2026, m) for m in range(1, 9)] + [(2025, 10), (2025, 11)])
    shock = {day: 0.03 for day in days}
    market = MarketDataService(scripted, provider=ScriptedProvider(beta=1.1, shock=shock))
    for day in days:
        add_event(scripted, dt.datetime(day.year, day.month, day.day, 15, 0, tzinfo=UTC))

    result = run_event_study(scripted, market, ticker="AAPL", event_type="tariff")

    assert result.n_events == len(days)
    same_day = result.windows["[+0,+0]"]
    assert same_day["caar"] == pytest.approx(0.03, abs=0.004)
    assert same_day["positive_pct"] == 100.0
    assert same_day["t_stat"] is not None and same_day["t_stat"] > 1.96
    assert same_day["significant_5pct"] is True


def test_event_study_finds_nothing_when_there_is_nothing(scripted):
    """No shock: the CAAR must be indistinguishable from zero."""
    days = first_sessions([(2026, m) for m in range(1, 9)])
    market = MarketDataService(scripted, provider=ScriptedProvider(beta=1.1))
    for day in days:
        add_event(scripted, dt.datetime(day.year, day.month, day.day, 15, 0, tzinfo=UTC))

    result = run_event_study(scripted, market, ticker="AAPL", event_type="tariff")
    same_day = result.windows["[+0,+0]"]
    assert abs(same_day["caar"]) < 0.005
    assert same_day["significant_5pct"] is False


def test_event_study_respects_the_before_cutoff(scripted):
    days = first_sessions([(2026, m) for m in range(1, 9)])
    market = MarketDataService(scripted, provider=ScriptedProvider())
    for day in days:
        add_event(scripted, dt.datetime(day.year, day.month, day.day, 15, 0, tzinfo=UTC))

    result = run_event_study(
        scripted,
        market,
        ticker="AAPL",
        before=dt.datetime(2026, 4, 1, tzinfo=UTC),
    )
    for entry in result.events:
        assert entry["source_timestamp"] < "2026-04-01"


def test_event_study_filters_by_event_type(scripted):
    market = MarketDataService(scripted, provider=ScriptedProvider())
    add_event(scripted, dt.datetime(2026, 3, 3, 15, 0, tzinfo=UTC), event_type="tariff")
    add_event(scripted, dt.datetime(2026, 4, 3, 15, 0, tzinfo=UTC), event_type="sanction")

    result = run_event_study(scripted, market, ticker="AAPL", event_type="sanction")
    assert result.n_events == 1
    assert result.events[0]["event_type"] == "sanction"


def test_event_study_flags_a_small_sample(scripted):
    market = MarketDataService(scripted, provider=ScriptedProvider())
    add_event(scripted, dt.datetime(2026, 3, 3, 15, 0, tzinfo=UTC))

    result = run_event_study(scripted, market, ticker="AAPL")
    assert result.sample_flag == "unreliable"
    assert any("illustrative" in note for note in result.notes)
    # A cross-sectional t-statistic on one observation would be meaningless.
    assert result.windows["[+0,+0]"]["t_stat"] is None


def test_event_study_lists_skipped_events_rather_than_hiding_them(scripted):
    """An event with no usable estimation window must be auditable."""
    market = MarketDataService(scripted, provider=ScriptedProvider())
    # 1990 is before the provider generates any history.
    add_event(scripted, dt.datetime(1990, 3, 5, 15, 0, tzinfo=UTC))

    result = run_event_study(scripted, market, ticker="AAPL")
    assert result.n_events == 0
    assert len(result.skipped) == 1
    assert result.skipped[0]["reason"]


def test_event_study_reports_every_default_window(scripted):
    days = first_sessions([(2026, m) for m in range(1, 7)])
    market = MarketDataService(scripted, provider=ScriptedProvider())
    for day in days:
        add_event(scripted, dt.datetime(day.year, day.month, day.day, 15, 0, tzinfo=UTC))

    result = run_event_study(scripted, market, ticker="AAPL")
    for window in DEFAULT_WINDOWS:
        assert window_label(window) in result.windows


def test_out_of_hours_event_is_anchored_to_the_next_session(scripted):
    market = MarketDataService(scripted, provider=ScriptedProvider())
    # Saturday 02:00 ET -> reaction day is the following Monday.
    add_event(scripted, dt.datetime(2026, 3, 7, 7, 0, tzinfo=UTC))

    result = run_event_study(scripted, market, ticker="AAPL")
    assert result.events[0]["basis"] == "next_open"
    assert result.events[0]["reaction_day"] == "2026-03-09"
