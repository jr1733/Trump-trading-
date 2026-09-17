#!/usr/bin/env python3
"""Probe every configured source and the market-data provider against the real
internet, and say which ones actually work.

This exists because of a specific gap: the build environment had no outbound
access to government or market sites, so no feed URL and no Stooq symbol in this
repository was ever confirmed against the live service. Every one of them is a
plausible guess. This script is how you find out which guesses were wrong,
before a silent empty feed costs you a week of missing events.

    make verify-sources              # everything
    make verify-sources ARGS="--sources whitehouse,news_rss"
    make verify-sources ARGS="--market-only"

It writes nothing to the database and creates no events: adapters are called
directly and their results counted, then discarded.

Exit status is 0 when everything that is *configured to run* works. A source
that needs a key it does not have, or that is manual-import only, is a
configuration state rather than a failure and does not fail the run -- it is
reported and skipped. A source listed in ENABLED_SOURCES that cannot be reached
or returns nothing usable **does** fail the run, so this is usable in CI or as a
post-deploy smoke test.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import traceback
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.market.base import MarketDataError  # noqa: E402
from app.market.service import build_provider  # noqa: E402
from app.sources.registry import ADAPTERS  # noqa: E402

OK = "OK"
NEEDS_KEY = "NEEDS KEY"
MANUAL_ONLY = "MANUAL ONLY"
FAILED = "FAILED"
EMPTY = "EMPTY"

# States that are configuration, not breakage: reported, never fatal.
NON_FATAL = {OK, NEEDS_KEY, MANUAL_ONLY}

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def colour(state: str) -> str:
    if not sys.stdout.isatty():
        return state
    if state == OK:
        return f"{GREEN}{state}{RESET}"
    if state in (NEEDS_KEY, MANUAL_ONLY, EMPTY):
        return f"{YELLOW}{state}{RESET}"
    return f"{RED}{state}{RESET}"


class Result:
    def __init__(self, name: str, state: str, detail: str = "") -> None:
        self.name = name
        self.state = state
        self.detail = detail

    #: Set for a failing *fallback* provider: worth reporting loudly, but it
    #: does not mean the app has no prices.
    non_fatal_override = False

    @property
    def fatal(self) -> bool:
        return self.state not in NON_FATAL and not self.non_fatal_override

    def render(self) -> str:
        return f"  {self.name:<22} {colour(self.state):<20} {self.detail}"


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
def check_source(key: str, *, verbose: bool = False) -> Result:
    cls = ADAPTERS.get(key)
    if cls is None:
        return Result(key, FAILED, "no adapter registered under this key")

    adapter = cls()
    if not adapter.enabled():
        # The adapter itself decides why it is off. `kind` distinguishes "give
        # me a key" from "there is no API to call and there never will be".
        state = MANUAL_ONLY if adapter.kind == "manual" else NEEDS_KEY
        return Result(key, state, _why_disabled(key))

    started = dt.datetime.now(dt.timezone.utc)
    try:
        items = adapter.fetch()
    except Exception as exc:
        if verbose:
            traceback.print_exc()
        return Result(key, FAILED, f"{type(exc).__name__}: {exc}"[:160])

    elapsed = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
    if not items:
        # Reachable but silent. Usually a feed that moved: the URL still serves
        # a page, it just is not the feed any more. This is the failure mode
        # that hides for weeks, so it is reported loudly rather than as OK.
        return Result(key, EMPTY, f"reachable in {elapsed:.1f}s but returned 0 items")

    newest = max(item.source_timestamp for item in items)
    age_hours = (dt.datetime.now(dt.timezone.utc) - newest).total_seconds() / 3600
    detail = f"{len(items):>3} items in {elapsed:.1f}s · newest {age_hours:.0f}h old"
    if verbose:
        detail += f"\n{DIM}      e.g. {items[0].title or items[0].text[:70]!r}{RESET}"
    return Result(key, OK, detail)


def _why_disabled(key: str) -> str:
    return {
        "congress": "set CONGRESS_API_KEY (free: api.congress.gov/sign-up)",
        "oge": "no OGE API exists; use the manual import or set OGE_FEED_URL",
        "truth_social": "no terms-compliant public feed; manual import only",
        "news_rss": "set NEWS_RSS_FEEDS",
    }.get(key, "not configured")


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------
def check_market(
    symbol: str, *, provider_name: str | None = None, days: int = 14, verbose: bool = False
) -> Result:
    # An explicit name bypasses the fallback chain, so each side of the chain is
    # probed on its own -- otherwise a working fallback would hide a broken
    # primary, which is exactly the thing this script exists to surface.
    provider = build_provider(provider_name)
    end = dt.date.today()
    start = end - dt.timedelta(days=days)

    try:
        bars = provider.fetch_daily(symbol, start, end)
    except MarketDataError as exc:
        if verbose:
            traceback.print_exc()
        return Result(symbol, FAILED, str(exc)[:160])
    except Exception as exc:
        if verbose:
            traceback.print_exc()
        return Result(symbol, FAILED, f"{type(exc).__name__}: {exc}"[:160])

    if not bars:
        return Result(symbol, EMPTY, f"no bars in the last {days} days")

    latest = max(bar.ts for bar in bars)
    # A bar stamped at today's open before the open has happened is normal (the
    # mock provider does it), so clamp rather than reporting a negative age.
    age_days = max(0, (dt.datetime.now(dt.timezone.utc) - latest).days)
    detail = f"{len(bars):>3} bars · latest {latest.date()} ({age_days}d ago)"
    if age_days > 5:
        # Five days covers a long weekend plus a holiday. Beyond that the symbol
        # mapping is probably resolving to something stale or wrong.
        return Result(symbol, EMPTY, detail + " · suspiciously stale, check the symbol mapping")
    return Result(symbol, OK, detail)


# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--sources", help="comma-separated keys (default: ENABLED_SOURCES)")
    parser.add_argument("--all-sources", action="store_true", help="probe every known adapter")
    parser.add_argument("--market-only", action="store_true")
    parser.add_argument("--sources-only", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true", help="show tracebacks and samples")
    args = parser.parse_args()

    results: list[Result] = []

    if not args.market_only:
        if args.all_sources:
            keys = list(ADAPTERS)
        elif args.sources:
            keys = [k.strip() for k in args.sources.split(",") if k.strip()]
        else:
            keys = list(settings.enabled_sources)

        print(f"\nSources ({', '.join(keys) or 'none enabled'})")
        for key in keys:
            result = check_source(key, verbose=args.verbose)
            results.append(result)
            print(result.render())

    if not args.sources_only:
        symbols = [settings.benchmark_symbol, *settings.market_context_symbols]
        seen: list[str] = []
        for symbol in symbols:
            if symbol.upper() not in seen:
                seen.append(symbol.upper())

        # Primary and fallback are probed separately, each bypassing the chain.
        # A working fallback masking a broken primary is precisely the silent
        # degradation this script exists to catch.
        providers = [(settings.market_data_provider, True)]
        fallback = (settings.market_data_fallback_provider or "").strip()
        if fallback and fallback.lower() != settings.market_data_provider.lower():
            providers.append((fallback, False))

        for provider_name, is_primary in providers:
            role = "primary" if is_primary else "fallback"
            print(f"\nMarket data -- {role}: {provider_name}")
            if provider_name.lower() == "mock":
                print(
                    f"  {DIM}mock provider: these bars are synthetic and always succeed."
                    f"\n  Set MARKET_DATA_PROVIDER=stooq to verify the real thing.{RESET}"
                )
            for symbol in seen:
                result = check_market(symbol, provider_name=provider_name, verbose=args.verbose)
                # A fallback failing is serious but not the same as having no
                # prices at all, so it is reported without failing the run.
                if not is_primary and result.fatal:
                    result = Result(
                        result.name, result.state, result.detail + " (fallback only)"
                    )
                    result.non_fatal_override = True
                results.append(result)
                print(result.render())

        if len(providers) == 1 and settings.market_data_provider.lower() != "mock":
            print(
                f"  {DIM}No fallback configured. Set MARKET_DATA_FALLBACK_PROVIDER so a"
                f"\n  single moved endpoint cannot leave the app with no prices at all.{RESET}"
            )

    failures = [r for r in results if r.fatal]
    print()
    if failures:
        print(f"{len(failures)} of {len(results)} checks failed:")
        for result in failures:
            print(f"  - {result.name}: {result.detail}")
        print(
            "\nA feed that moved is the usual cause. Every URL is configurable "
            "(WHITEHOUSE_FEEDS, NEWS_RSS_FEEDS) and every Stooq symbol mapping "
            "lives in backend/app/market/stooq_provider.py."
        )
        return 1

    print(f"All {len(results)} checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
