"""Alpha Vantage daily bars -- the fallback for when Stooq does not work.

Why a second provider exists at all: no feed URL or symbol mapping in this
repository has ever been confirmed against a live service, Stooq included. If
Stooq's CSV endpoint has moved, changed shape, or started refusing the
request, the app has no prices at all -- and prices are what every statistic in
it is computed from. One unverified provider is a single point of failure for
the entire tool.

Alpha Vantage was chosen over the obvious alternative (the undocumented Yahoo
Finance chart endpoint) on terms-of-service grounds: it is a documented public
API with a published free tier, whereas the Yahoo endpoint is an internal one
that its terms do not permit automated use of. The same rule that keeps this
app off Truth Social applies here.

It needs a key, which Stooq does not, so it is a *fallback* rather than the
default: `MARKET_DATA_API_KEY`, free from alphavantage.co/support/#api-key.

**Daily bars only.** `supports_intraday()` is False, so intraday horizons stay
off, exactly as with Stooq. Intraday data exists on Alpha Vantage's paid tiers
and this adapter deliberately does not reach for it -- a horizon that silently
appears when you upgrade a billing plan is worse than one that is consistently
absent.

**Free-tier rate limits are low and Alpha Vantage has changed them more than
once.** Treat a burst of failures here as "out of quota" rather than "broken",
and see `make verify-sources`.
"""

from __future__ import annotations

import datetime as dt

import httpx

from ..config import settings
from .base import Bar, MarketDataError, MarketDataProvider
from .calendar import EASTERN, REGULAR_OPEN

API_URL = "https://www.alphavantage.co/query"

#: Alpha Vantage quotes indices and yields differently from ordinary equities,
#: and not all of them are available on the free tier. These are best guesses,
#: unverified like everything else here -- `make verify-sources` is how you find
#: out which are wrong.
_SYMBOL_OVERRIDES = {
    # Alpha Vantage has no free VIX or 10-year-yield series. The ETF proxies
    # below track them closely enough for a benchmark, and naming the
    # substitution here beats silently returning nothing.
    "VIX": "VIXY",   # short-term VIX futures ETF
    "TNX": "IEF",    # 7-10 year Treasury ETF -- a PRICE, not a yield
}

#: Symbols whose fallback series is a proxy rather than the thing itself. The
#: pipeline shows the provider next to every chart, so a reader can tell.
PROXY_SYMBOLS = frozenset(_SYMBOL_OVERRIDES)


def _api_symbol(symbol: str) -> str:
    return _SYMBOL_OVERRIDES.get(symbol.upper(), symbol.upper())


class AlphaVantageProvider(MarketDataProvider):
    name = "alphavantage"

    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None) -> None:
        self.api_key = api_key if api_key is not None else settings.market_data_api_key
        self._client = client

    def available(self) -> bool:
        """No key is a configuration state, not a failure."""
        return bool(self.api_key)

    def supports_intraday(self) -> bool:
        return False

    def _get(self, params: dict[str, str]) -> dict:
        if not self.available():
            raise MarketDataError("MARKET_DATA_API_KEY is not set")
        headers = {"User-Agent": settings.http_user_agent}
        try:
            if self._client is not None:
                response = self._client.get(API_URL, params=params, headers=headers)
            else:
                with httpx.Client(timeout=settings.http_timeout_seconds) as client:
                    response = client.get(API_URL, params=params, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise MarketDataError(f"alphavantage request failed: {exc}") from exc
        except ValueError as exc:
            raise MarketDataError(f"alphavantage returned non-JSON: {exc}") from exc

    def fetch_daily(self, symbol: str, start: dt.date, end: dt.date) -> list[Bar]:
        payload = self._get(
            {
                "function": "TIME_SERIES_DAILY",
                "symbol": _api_symbol(symbol),
                # `full` reaches back 20+ years; `compact` is the last 100 days.
                # The estimation window for the market model is 180 trading
                # days, so compact is not enough.
                "outputsize": "full" if (end - start).days > 90 else "compact",
                "apikey": self.api_key,
            }
        )

        # Alpha Vantage signals every error with HTTP 200 and a JSON key, so the
        # status code tells you nothing. These three are the ones that happen.
        for key, label in (
            ("Error Message", "rejected the symbol"),
            ("Note", "rate limited"),
            ("Information", "rate limited or requires a paid plan"),
        ):
            if key in payload:
                raise MarketDataError(f"alphavantage {label}: {str(payload[key])[:160]}")

        series = payload.get("Time Series (Daily)")
        if not isinstance(series, dict) or not series:
            raise MarketDataError(
                f"alphavantage returned no daily series for {symbol}: {str(payload)[:160]}"
            )

        bars: list[Bar] = []
        for day_str, row in series.items():
            try:
                day = dt.date.fromisoformat(day_str)
            except ValueError:
                continue
            if not (start <= day <= end):
                continue
            try:
                close = float(row["4. close"])
                bars.append(
                    Bar(
                        symbol=symbol.upper(),
                        interval="1d",
                        ts=dt.datetime.combine(day, REGULAR_OPEN, tzinfo=EASTERN).astimezone(
                            dt.timezone.utc
                        ),
                        open=float(row["1. open"]),
                        high=float(row["2. high"]),
                        low=float(row["3. low"]),
                        close=close,
                        # TIME_SERIES_DAILY is unadjusted. Saying so beats
                        # labelling a raw close "adjusted" -- a split would then
                        # read as a 50% one-day move.
                        adjusted_close=close,
                        volume=float(row.get("5. volume") or 0.0),
                    )
                )
            except (KeyError, ValueError, TypeError):
                # One malformed row must not discard the whole series.
                continue

        if not bars:
            raise MarketDataError(
                f"alphavantage returned no rows for {symbol} between {start} and {end}"
            )
        return sorted(bars, key=lambda b: b.ts)

    def fetch_intraday(
        self, symbol: str, start: dt.datetime, end: dt.datetime, interval: str
    ) -> list[Bar]:
        raise MarketDataError("alphavantage intraday is not enabled in this adapter")
