"""The market-data fallback chain and the Alpha Vantage provider.

Stooq has never been verified against the live service -- the build environment
blocked outbound access to it -- which makes a single provider a single point of
failure for every number this app computes.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from app.config import settings
from app.market.alphavantage_provider import AlphaVantageProvider
from app.market.base import Bar, MarketDataError, MarketDataProvider
from app.market.chain import ChainedMarketDataProvider
from app.market.service import build_provider

UTC = dt.timezone.utc


def bar(symbol="SPY", day=dt.date(2026, 3, 4), close=100.0) -> Bar:
    return Bar(
        symbol=symbol,
        interval="1d",
        ts=dt.datetime.combine(day, dt.time(14, 30), tzinfo=UTC),
        open=close,
        high=close,
        low=close,
        close=close,
        adjusted_close=close,
        volume=1_000.0,
    )


class Working(MarketDataProvider):
    def __init__(self, name="working", intraday=False):
        self.name = name
        self.calls = 0
        self._intraday = intraday

    def supports_intraday(self):
        return self._intraday

    def intraday_intervals(self):
        return ["1m", "5m"] if self._intraday else []

    def fetch_daily(self, symbol, start, end):
        self.calls += 1
        return [bar(symbol)]

    def fetch_intraday(self, symbol, start, end, interval):
        self.calls += 1
        return [bar(symbol)]


class Broken(MarketDataProvider):
    def __init__(self, name="broken", intraday=False):
        self.name = name
        self.calls = 0
        self._intraday = intraday

    def supports_intraday(self):
        return self._intraday

    def intraday_intervals(self):
        return ["1m", "5m"] if self._intraday else []

    def fetch_daily(self, symbol, start, end):
        self.calls += 1
        raise MarketDataError(f"{self.name} is down")

    def fetch_intraday(self, symbol, start, end, interval):
        self.calls += 1
        raise MarketDataError(f"{self.name} is down")


WINDOW = (dt.date(2026, 3, 1), dt.date(2026, 3, 10))


# --- the chain ------------------------------------------------------------
def test_the_primary_is_used_when_it_works():
    primary, fallback = Working("stooq"), Working("alphavantage")
    chain = ChainedMarketDataProvider(primary, fallback)

    assert chain.fetch_daily("SPY", *WINDOW)
    assert primary.calls == 1
    assert fallback.calls == 0, "the fallback must not be called when the primary works"


def test_the_fallback_answers_when_the_primary_raises():
    primary, fallback = Broken("stooq"), Working("alphavantage")
    chain = ChainedMarketDataProvider(primary, fallback)

    assert chain.fetch_daily("SPY", *WINDOW)
    assert primary.calls == 1 and fallback.calls == 1


def test_the_provider_name_follows_whichever_answered():
    """`name` is written to market_prices.provider, so it has to be the real
    source -- a row labelled "chain" makes the provenance useless."""
    chain = ChainedMarketDataProvider(Broken("stooq"), Working("alphavantage"))
    assert chain.name == "stooq"  # before any call, the intended primary
    chain.fetch_daily("SPY", *WINDOW)
    assert chain.name == "alphavantage"


def test_the_primary_is_retried_on_the_next_call():
    """No circuit breaker to get wrong: recovery is automatic."""

    class Flaky(MarketDataProvider):
        name = "flaky"

        def __init__(self):
            self.calls = 0

        def supports_intraday(self):
            return False

        def fetch_daily(self, symbol, start, end):
            self.calls += 1
            if self.calls == 1:
                raise MarketDataError("transient")
            return [bar(symbol)]

    primary = Flaky()
    chain = ChainedMarketDataProvider(primary, Working("alphavantage"))

    chain.fetch_daily("SPY", *WINDOW)
    assert chain.name == "alphavantage"

    chain.fetch_daily("SPY", *WINDOW)
    assert chain.name == "flaky", "the primary must be tried again, not written off"


def test_both_failing_names_both_providers():
    chain = ChainedMarketDataProvider(Broken("stooq"), Broken("alphavantage"))
    with pytest.raises(MarketDataError) as exc:
        chain.fetch_daily("SPY", *WINDOW)

    message = str(exc.value)
    assert "stooq" in message and "alphavantage" in message


def test_intraday_support_is_the_and_of_the_chain():
    """Not the OR: a horizon that appears only when one provider answers would
    give the same event a 5-minute return one day and not the next."""
    assert ChainedMarketDataProvider(
        Working(intraday=True), Working(intraday=False)
    ).supports_intraday() is False
    assert ChainedMarketDataProvider(
        Working(intraday=True), Working(intraday=True)
    ).supports_intraday() is True


def test_intraday_intervals_are_the_intersection():
    chain = ChainedMarketDataProvider(Working(intraday=True), Working(intraday=True))
    assert chain.intraday_intervals() == ["1m", "5m"]
    assert ChainedMarketDataProvider(
        Working(intraday=True), Working(intraday=False)
    ).intraday_intervals() == []


# --- wiring ---------------------------------------------------------------
def test_no_fallback_configured_gives_a_bare_provider(monkeypatch):
    monkeypatch.setattr(settings, "market_data_provider", "stooq")
    monkeypatch.setattr(settings, "market_data_fallback_provider", None)
    assert not isinstance(build_provider(), ChainedMarketDataProvider)


def test_one_env_var_turns_the_chain_on(monkeypatch):
    monkeypatch.setattr(settings, "market_data_provider", "stooq")
    monkeypatch.setattr(settings, "market_data_fallback_provider", "alphavantage")

    provider = build_provider()
    assert isinstance(provider, ChainedMarketDataProvider)
    assert provider.primary.name == "stooq"
    assert provider.fallback.name == "alphavantage"


def test_a_fallback_equal_to_the_primary_is_ignored(monkeypatch):
    monkeypatch.setattr(settings, "market_data_provider", "stooq")
    monkeypatch.setattr(settings, "market_data_fallback_provider", "stooq")
    assert not isinstance(build_provider(), ChainedMarketDataProvider)


def test_an_explicit_name_bypasses_the_chain(monkeypatch):
    """verify-sources relies on this to probe each side separately -- a working
    fallback masking a broken primary is the thing it exists to catch."""
    monkeypatch.setattr(settings, "market_data_provider", "stooq")
    monkeypatch.setattr(settings, "market_data_fallback_provider", "alphavantage")

    assert build_provider("stooq").name == "stooq"
    assert build_provider("alphavantage").name == "alphavantage"
    assert not isinstance(build_provider("stooq"), ChainedMarketDataProvider)


# --- Alpha Vantage --------------------------------------------------------
def av_client(payload, status=200):
    def handler(request):
        return httpx.Response(status, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


DAILY = {
    "Time Series (Daily)": {
        "2026-03-04": {
            "1. open": "100.0", "2. high": "101.0", "3. low": "99.0",
            "4. close": "100.5", "5. volume": "1000",
        },
        "2026-03-05": {
            "1. open": "100.5", "2. high": "102.0", "3. low": "100.0",
            "4. close": "101.5", "5. volume": "1200",
        },
    }
}


def test_alphavantage_parses_daily_bars():
    provider = AlphaVantageProvider(api_key="k", client=av_client(DAILY))
    bars = provider.fetch_daily("SPY", dt.date(2026, 3, 1), dt.date(2026, 3, 10))

    assert [b.close for b in bars] == [100.5, 101.5]
    assert bars[0].ts < bars[1].ts, "bars come back in chronological order"
    assert provider.supports_intraday() is False


def test_alphavantage_filters_to_the_requested_window():
    provider = AlphaVantageProvider(api_key="k", client=av_client(DAILY))
    bars = provider.fetch_daily("SPY", dt.date(2026, 3, 5), dt.date(2026, 3, 10))
    assert [b.close for b in bars] == [101.5]


def test_alphavantage_without_a_key_is_a_clean_error():
    with pytest.raises(MarketDataError, match="MARKET_DATA_API_KEY"):
        AlphaVantageProvider(api_key=None).fetch_daily("SPY", *WINDOW)


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"Error Message": "Invalid API call"}, "rejected the symbol"),
        ({"Note": "call frequency"}, "rate limited"),
        ({"Information": "premium endpoint"}, "rate limited"),
    ],
)
def test_alphavantage_errors_arrive_as_http_200_and_are_caught(payload, expected):
    """Alpha Vantage signals every error with a 200 and a JSON key, so the
    status code tells you nothing at all."""
    provider = AlphaVantageProvider(api_key="k", client=av_client(payload))
    with pytest.raises(MarketDataError, match=expected):
        provider.fetch_daily("SPY", *WINDOW)


def test_alphavantage_rejects_an_empty_series():
    provider = AlphaVantageProvider(api_key="k", client=av_client({"Time Series (Daily)": {}}))
    with pytest.raises(MarketDataError, match="no daily series"):
        provider.fetch_daily("SPY", *WINDOW)


def test_one_malformed_row_does_not_discard_the_series():
    payload = {
        "Time Series (Daily)": {
            "2026-03-04": {"1. open": "100.0", "2. high": "101.0",
                           "3. low": "99.0", "4. close": "100.5", "5. volume": "1000"},
            "2026-03-05": {"1. open": "oops"},           # missing keys
            "not-a-date": {"4. close": "1.0"},           # unparseable key
        }
    }
    provider = AlphaVantageProvider(api_key="k", client=av_client(payload))
    bars = provider.fetch_daily("SPY", dt.date(2026, 3, 1), dt.date(2026, 3, 10))
    assert len(bars) == 1


def test_index_symbols_map_to_proxies():
    from app.market.alphavantage_provider import PROXY_SYMBOLS, _api_symbol

    assert _api_symbol("SPY") == "SPY"
    assert _api_symbol("VIX") != "VIX", "no free VIX series; a proxy is substituted"
    assert "VIX" in PROXY_SYMBOLS and "TNX" in PROXY_SYMBOLS


def test_alphavantage_intraday_is_refused_rather_than_faked():
    provider = AlphaVantageProvider(api_key="k", client=av_client(DAILY))
    with pytest.raises(MarketDataError):
        provider.fetch_intraday("SPY", dt.datetime.now(UTC), dt.datetime.now(UTC), "5m")
