"""Backtesting: metrics, splits, look-ahead prevention, contamination labelling."""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools

import pytest

from app.market.base import Bar, MarketDataProvider
from app.market.calendar import next_trading_day, session_open_utc
from app.market.service import MarketDataService
from app.models import ClaudeAnalysis, Event, EventTicker, Ticker
from app.pipeline.backtest import (
    MIN_SHARPE_SAMPLE,
    BacktestParams,
    assign_split,
    max_drawdown,
    overlap_fraction,
    point_in_time_signal,
    run_backtest,
    sharpe_ratio,
    split_boundaries,
    store_run,
    summarise,
)
from app.pipeline.event_study import trading_days_between
from app.seed import loader

UTC = dt.timezone.utc


# --- metrics --------------------------------------------------------------
def test_max_drawdown_of_a_rising_series_is_zero():
    assert max_drawdown([0.01, 0.02, 0.01]) == 0.0


def test_max_drawdown_measures_peak_to_trough():
    # cumulative: 0.10, 0.05, -0.05, 0.05 -> peak 0.10, trough -0.05
    assert max_drawdown([0.10, -0.05, -0.10, 0.10]) == pytest.approx(-0.15)


def test_max_drawdown_of_nothing_is_zero():
    assert max_drawdown([]) == 0.0


def test_sharpe_is_withheld_on_a_small_sample():
    assert sharpe_ratio([0.01] * (MIN_SHARPE_SAMPLE - 1), 5) is None


def test_sharpe_is_withheld_without_dispersion():
    assert sharpe_ratio([0.01] * (MIN_SHARPE_SAMPLE + 5), 5) is None


def test_sharpe_is_positive_for_a_positive_mean():
    returns = [0.02, 0.01, -0.005, 0.03, 0.015] * 6
    value = sharpe_ratio(returns, 5)
    assert value is not None and value > 0


def test_sharpe_scales_with_the_holding_period():
    """A shorter holding period means more periods per year, so a larger annualised figure."""
    returns = [0.02, 0.01, -0.005, 0.03, 0.015] * 6
    assert sharpe_ratio(returns, 1) > sharpe_ratio(returns, 20)


def test_summarise_on_an_empty_set_reports_nothing_rather_than_zeroes():
    block = summarise([], 5)
    assert block["n"] == 0
    assert block["mean"] is None and block["win_rate"] is None and block["sharpe"] is None


# --- splits ---------------------------------------------------------------
def test_splits_are_chronological_and_contiguous():
    params = BacktestParams(start=dt.date(2024, 1, 1), end=dt.date(2027, 1, 1))
    boundaries = split_boundaries(params)

    assert [name for name, _, _ in boundaries] == ["train", "validation", "test"]
    assert boundaries[0][1] == params.start
    assert boundaries[-1][2] == params.end
    for earlier, later in zip(boundaries, boundaries[1:]):
        assert earlier[2] == later[1], "splits must not leave a gap or overlap"


def test_split_assignment_follows_the_boundaries():
    params = BacktestParams(start=dt.date(2024, 1, 1), end=dt.date(2027, 1, 1))
    boundaries = split_boundaries(params)
    assert assign_split(dt.date(2024, 6, 1), boundaries) == "train"
    assert assign_split(dt.date(2026, 12, 1), boundaries) == "test"


# --- overlap --------------------------------------------------------------
class FakeObservation:
    def __init__(self, reaction_day: dt.date, exit_day: dt.date) -> None:
        self.reaction_day = reaction_day
        self.exit_day = exit_day


def test_overlap_fraction_detects_overlapping_windows():
    rows = [
        FakeObservation(dt.date(2026, 1, 5), dt.date(2026, 1, 9)),
        FakeObservation(dt.date(2026, 1, 7), dt.date(2026, 1, 13)),  # overlaps
        FakeObservation(dt.date(2026, 2, 2), dt.date(2026, 2, 6)),   # does not
    ]
    assert overlap_fraction(rows) == pytest.approx(0.5)


def test_overlap_fraction_of_a_single_observation_is_zero():
    assert overlap_fraction([FakeObservation(dt.date(2026, 1, 5), dt.date(2026, 1, 9))]) == 0.0


# --- a controlled market -------------------------------------------------
class RisingProvider(MarketDataProvider):
    """Every symbol rises a fixed amount each session. Deterministic by design."""

    name = "rising"
    EPOCH = dt.date(2023, 6, 1)

    def __init__(self, daily: float = 0.01) -> None:
        self.daily = daily

    def supports_intraday(self) -> bool:
        return False

    def fetch_daily(self, symbol: str, start: dt.date, end: dt.date) -> list[Bar]:
        if end < self.EPOCH:
            return []
        bars: list[Bar] = []
        price = 100.0
        for day in trading_days_between(self.EPOCH, end):
            price *= 1.0 + self.daily
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


_EVENT_COUNTER = itertools.count()


def add_event(
    db,
    when: dt.datetime,
    *,
    ticker="AAPL",
    event_type="tariff",
    text="Tariffs on consumer electronics are under review.",
    sentiment: float | None = None,
) -> Event:
    # A counter, not the date: two calendar days can round to the same trading
    # day, and the content hash is unique in the schema.
    serial = next(_EVENT_COUNTER)
    event = Event(
        source_key="mock",
        external_id=f"bt-{when.isoformat()}-{ticker}-{serial}",
        title="Tariff statement",
        text=f"{text} (ref {serial})",
        content_hash=hashlib.sha256(f"{when}{ticker}{text}{serial}".encode()).hexdigest(),
        event_type=event_type,
        source_timestamp=when,
        relevant=True,
        analysis_status="COMPLETE" if sentiment is not None else "SKIPPED",
    )
    db.add(event)
    db.flush()
    db.add(EventTicker(event_id=event.id, ticker=ticker, confidence="HIGH"))
    if sentiment is not None:
        db.add(
            ClaudeAnalysis(
                event_id=event.id,
                content_hash=event.content_hash,
                model="test-model",
                status="COMPLETE",
                sentiment=sentiment,
                confidence=0.8,
                parsed={"sentiment": sentiment, "confidence": 0.8},
            )
        )
    db.commit()
    db.refresh(event)
    return event


@pytest.fixture
def market_db(db):
    db.add(Ticker(symbol="AAPL", name="Apple Inc.", sector="Technology", sector_etf="XLK"))
    db.add(Ticker(symbol="SPY", name="S&P 500", asset_class="etf"))
    db.add(Ticker(symbol="XLK", name="Tech sector", asset_class="etf"))
    db.commit()
    return db


def seed_events(db, count: int = 12, **kwargs) -> list[Event]:
    days = [
        next_trading_day(dt.date(2025, 1, 1) + dt.timedelta(days=30 * i), inclusive=True)
        for i in range(count)
    ]
    return [
        add_event(db, dt.datetime(d.year, d.month, d.day, 15, 0, tzinfo=UTC), **kwargs)
        for d in days
    ]


# --- end to end -----------------------------------------------------------
def test_backtest_recovers_a_known_direction(market_db):
    """Prices rise every session, so a long-signalled set must show positive returns."""
    seed_events(market_db, 12, text="A tremendous deal, a great agreement, strong growth")
    market = MarketDataService(market_db, provider=RisingProvider(0.01))

    params = BacktestParams(
        start=dt.date(2025, 1, 1), end=dt.date(2026, 3, 1), min_signal=0.0, holding_days=5
    )
    result = run_backtest(market_db, params, market=market)

    assert result.overall["n"] > 0
    longs = [o for o in result.observations if o.direction > 0]
    assert longs, "positive lexicon sentiment should produce long-direction observations"
    # 5 sessions of +1% compounding from the pre-event close.
    assert longs[0].raw_return == pytest.approx(1.01**5 - 1, abs=1e-6)
    assert longs[0].aligned_return == pytest.approx(longs[0].raw_return)


def test_short_direction_flips_the_sign(market_db):
    seed_events(market_db, 12, text="Terrible, a disaster, sanctions and a crackdown, unfair")
    market = MarketDataService(market_db, provider=RisingProvider(0.01))

    result = run_backtest(
        market_db,
        BacktestParams(
            start=dt.date(2025, 1, 1), end=dt.date(2026, 3, 1), min_signal=0.0, holding_days=5
        ),
        market=market,
    )
    shorts = [o for o in result.observations if o.direction < 0]
    assert shorts, "negative sentiment should produce short-direction observations"
    # The price rose, so a short-direction observation records a negative result.
    assert shorts[0].raw_return > 0
    assert shorts[0].aligned_return == pytest.approx(-shorts[0].raw_return)


def test_min_signal_filters_observations(market_db):
    seed_events(market_db, 12)
    market = MarketDataService(market_db, provider=RisingProvider())

    permissive = run_backtest(
        market_db,
        BacktestParams(start=dt.date(2025, 1, 1), end=dt.date(2026, 3, 1), min_signal=0.0),
        market=market,
    )
    strict = run_backtest(
        market_db,
        BacktestParams(start=dt.date(2025, 1, 1), end=dt.date(2026, 3, 1), min_signal=0.99),
        market=market,
    )
    assert permissive.overall["n"] > strict.overall["n"]
    assert strict.overall["n"] == 0


def test_date_range_bounds_the_sample(market_db):
    seed_events(market_db, 12)
    market = MarketDataService(market_db, provider=RisingProvider())

    result = run_backtest(
        market_db,
        BacktestParams(
            start=dt.date(2025, 6, 1), end=dt.date(2025, 12, 31), min_signal=0.0
        ),
        market=market,
    )
    for observation in result.observations:
        assert dt.date(2025, 6, 1) <= observation.source_timestamp.date() <= dt.date(2025, 12, 31)


def test_ticker_and_event_type_filters(market_db):
    seed_events(market_db, 6, ticker="AAPL", event_type="tariff")
    seed_events(market_db, 6, ticker="SPY", event_type="sanction")
    market = MarketDataService(market_db, provider=RisingProvider())

    result = run_backtest(
        market_db,
        BacktestParams(
            start=dt.date(2025, 1, 1),
            end=dt.date(2026, 3, 1),
            ticker="SPY",
            event_type="sanction",
            min_signal=0.0,
        ),
        market=market,
    )
    assert result.observations
    assert {o.ticker for o in result.observations} == {"SPY"}
    assert {o.event_type for o in result.observations} == {"sanction"}


def test_holding_period_changes_the_exit(market_db):
    seed_events(market_db, 6)
    market = MarketDataService(market_db, provider=RisingProvider(0.01))

    short = run_backtest(
        market_db,
        BacktestParams(
            start=dt.date(2025, 1, 1), end=dt.date(2026, 3, 1), min_signal=0.0, holding_days=1
        ),
        market=market,
    )
    long = run_backtest(
        market_db,
        BacktestParams(
            start=dt.date(2025, 1, 1), end=dt.date(2026, 3, 1), min_signal=0.0, holding_days=10
        ),
        market=market,
    )
    assert abs(short.observations[0].raw_return) < abs(long.observations[0].raw_return)


# --- look-ahead prevention ------------------------------------------------
def test_point_in_time_signal_cannot_see_later_events(market_db):
    """The signal for an early event must not be built from later ones."""
    events = seed_events(market_db, 8)
    market = MarketDataService(market_db, provider=RisingProvider())

    first = point_in_time_signal(
        market_db,
        events[0],
        "AAPL",
        market=market,
        embeddings=None,
        sentiment_mode="rule_based",
        ticker_confidence="HIGH",
    )
    last = point_in_time_signal(
        market_db,
        events[-1],
        "AAPL",
        market=market,
        embeddings=None,
        sentiment_mode="rule_based",
        ticker_confidence="HIGH",
    )
    assert first.sample_size == 0, "the earliest event has no prior comparable events"
    assert last.sample_size > first.sample_size


def test_backtest_does_not_write_into_the_live_tables(market_db):
    """A backtest must leave `signals` and `historical_event_matches` untouched."""
    from sqlalchemy import func, select

    from app.models import HistoricalEventMatch, Signal

    seed_events(market_db, 8)
    market = MarketDataService(market_db, provider=RisingProvider())

    run_backtest(
        market_db,
        BacktestParams(start=dt.date(2025, 1, 1), end=dt.date(2026, 3, 1), min_signal=0.0),
        market=market,
    )
    assert market_db.execute(select(func.count(Signal.id))).scalar() == 0
    assert market_db.execute(select(func.count(HistoricalEventMatch.id))).scalar() == 0


# --- LLM contamination ----------------------------------------------------
def test_rule_based_mode_is_not_flagged_as_contaminated(market_db):
    seed_events(market_db, 6)
    market = MarketDataService(market_db, provider=RisingProvider())

    result = run_backtest(
        market_db,
        BacktestParams(
            start=dt.date(2025, 1, 1),
            end=dt.date(2026, 3, 1),
            min_signal=0.0,
            sentiment_mode="rule_based",
        ),
        market=market,
    )
    assert result.llm_contaminated is False
    assert any("No language model was involved" in note for note in result.notes)
    assert not any("CONTAMINATED" in warning for warning in result.warnings)


def test_llm_mode_is_flagged_as_contaminated(market_db):
    seed_events(market_db, 6, sentiment=0.7)
    market = MarketDataService(market_db, provider=RisingProvider())

    result = run_backtest(
        market_db,
        BacktestParams(
            start=dt.date(2025, 1, 1),
            end=dt.date(2026, 3, 1),
            min_signal=0.0,
            sentiment_mode="llm",
        ),
        market=market,
    )
    assert result.llm_contaminated is True
    assert any("POTENTIALLY CONTAMINATED" in warning for warning in result.warnings)


def test_llm_mode_skips_events_with_no_model_reading(market_db):
    """It must not silently fall back to the lexicon and call the result LLM-scored."""
    seed_events(market_db, 6)  # no ClaudeAnalysis rows
    market = MarketDataService(market_db, provider=RisingProvider())

    result = run_backtest(
        market_db,
        BacktestParams(
            start=dt.date(2025, 1, 1),
            end=dt.date(2026, 3, 1),
            min_signal=0.0,
            sentiment_mode="llm",
        ),
        market=market,
    )
    assert result.overall["n"] == 0
    assert result.skipped
    assert all("no model analysis" in row["reason"] for row in result.skipped)


def test_the_two_modes_can_disagree(market_db):
    """A positive model reading on negative-sounding text must change the direction."""
    seed_events(
        market_db,
        8,
        text="Terrible, a disaster, sanctions and a crackdown",
        sentiment=0.9,
    )
    market = MarketDataService(market_db, provider=RisingProvider())

    def directions(mode: str) -> set[int]:
        result = run_backtest(
            market_db,
            BacktestParams(
                start=dt.date(2025, 1, 1),
                end=dt.date(2026, 3, 1),
                min_signal=0.0,
                sentiment_mode=mode,
            ),
            market=market,
        )
        return {o.direction for o in result.observations}

    assert directions("rule_based") == {-1}
    assert directions("llm") == {1}


# --- warnings and persistence --------------------------------------------
def test_overlap_warning_fires_on_dense_events(market_db):
    days = [
        next_trading_day(dt.date(2025, 3, 3) + dt.timedelta(days=i), inclusive=True)
        for i in range(0, 24, 2)
    ]
    for day in days:
        add_event(market_db, dt.datetime(day.year, day.month, day.day, 15, 0, tzinfo=UTC))
    market = MarketDataService(market_db, provider=RisingProvider())

    result = run_backtest(
        market_db,
        BacktestParams(
            start=dt.date(2025, 1, 1),
            end=dt.date(2026, 3, 1),
            min_signal=0.0,
            holding_days=10,
        ),
        market=market,
    )
    assert result.overall["overlap_fraction"] > 0.25
    assert any("overlap" in warning for warning in result.warnings)


def test_small_sample_is_warned_about(market_db):
    seed_events(market_db, 2)
    market = MarketDataService(market_db, provider=RisingProvider())

    result = run_backtest(
        market_db,
        BacktestParams(start=dt.date(2025, 1, 1), end=dt.date(2026, 3, 1), min_signal=0.0),
        market=market,
    )
    assert any("Nothing here is a measurement" in warning for warning in result.warnings)


def test_notes_always_state_what_is_not_modelled(market_db):
    seed_events(market_db, 4)
    market = MarketDataService(market_db, provider=RisingProvider())

    result = run_backtest(
        market_db,
        BacktestParams(start=dt.date(2025, 1, 1), end=dt.date(2026, 3, 1), min_signal=0.0),
        market=market,
    )
    blob = " ".join(result.notes)
    assert "No costs" in blob
    assert "point-in-time" in blob


def test_store_run_persists_the_parameters_and_flag(market_db):
    user = loader.seed_user(market_db)
    seed_events(market_db, 4, sentiment=0.5)
    market = MarketDataService(market_db, provider=RisingProvider())

    params = BacktestParams(
        start=dt.date(2025, 1, 1),
        end=dt.date(2026, 3, 1),
        min_signal=0.0,
        sentiment_mode="llm",
    )
    result = run_backtest(market_db, params, market=market)
    run = store_run(market_db, user.id, result)

    assert run.llm_contaminated is True
    assert run.sentiment_mode == "llm"
    assert run.params["min_signal"] == 0.0
    assert run.results["overall"]["n"] == result.overall["n"]
    assert run.status == "COMPLETE"
