"""Timestamps, market hours, holidays, and the 2 a.m. event rule."""

from __future__ import annotations

import datetime as dt

import pytest

from app.market import calendar as mcal

UTC = dt.timezone.utc


def eastern(year, month, day, hour, minute=0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=mcal.EASTERN)


def test_weekend_is_not_a_trading_day():
    assert mcal.is_trading_day(dt.date(2026, 3, 7)) is False  # Saturday
    assert mcal.is_trading_day(dt.date(2026, 3, 8)) is False  # Sunday
    assert mcal.is_trading_day(dt.date(2026, 3, 9)) is True   # Monday


@pytest.mark.parametrize(
    "day",
    [
        dt.date(2026, 1, 1),    # New Year's Day
        dt.date(2026, 1, 19),   # MLK Jr Day (3rd Monday)
        dt.date(2026, 2, 16),   # Washington's Birthday (3rd Monday)
        dt.date(2026, 4, 3),    # Good Friday
        dt.date(2026, 5, 25),   # Memorial Day (last Monday)
        dt.date(2026, 6, 19),   # Juneteenth
        dt.date(2026, 7, 3),    # Independence Day observed (Jul 4 is a Saturday)
        dt.date(2026, 9, 7),    # Labor Day
        dt.date(2026, 11, 26),  # Thanksgiving
        dt.date(2026, 12, 25),  # Christmas
    ],
)
def test_market_holidays_are_closed(day):
    assert mcal.is_trading_day(day) is False, f"{day} should be a market holiday"


def test_early_close_day():
    friday_after_thanksgiving = dt.date(2026, 11, 27)
    assert mcal.session_close(friday_after_thanksgiving) == mcal.EARLY_CLOSE
    assert mcal.session_close(dt.date(2026, 11, 30)) == mcal.REGULAR_CLOSE


@pytest.mark.parametrize(
    "moment,expected",
    [
        (eastern(2026, 3, 4, 10, 0), "regular"),
        (eastern(2026, 3, 4, 9, 29), "premarket"),
        (eastern(2026, 3, 4, 5, 0), "premarket"),
        (eastern(2026, 3, 4, 16, 30), "afterhours"),
        (eastern(2026, 3, 4, 2, 0), "closed"),
        (eastern(2026, 3, 4, 21, 0), "closed"),
        (eastern(2026, 3, 7, 11, 0), "closed"),   # Saturday
        (eastern(2026, 1, 1, 11, 0), "closed"),   # holiday
    ],
)
def test_classify_sessions(moment, expected):
    assert mcal.classify(moment) == expected


def test_classify_requires_timezone_aware_input():
    with pytest.raises(ValueError):
        mcal.classify(dt.datetime(2026, 3, 4, 10, 0))


def test_intraday_event_anchors_to_itself():
    moment = eastern(2026, 3, 4, 11, 15)
    anchor, basis = mcal.anchor(moment)
    assert basis == "intraday"
    assert anchor == moment.astimezone(UTC)


def test_two_am_event_maps_to_the_next_session_open():
    """A 2 a.m. post has no 5-minute return; it maps to the next open and says so."""
    anchor, basis = mcal.anchor(eastern(2026, 3, 4, 2, 0))
    assert basis == "next_open"
    assert anchor == mcal.session_open_utc(dt.date(2026, 3, 4))


def test_after_hours_event_maps_to_the_next_trading_day():
    anchor, basis = mcal.anchor(eastern(2026, 3, 4, 18, 30))
    assert basis == "next_open"
    assert anchor == mcal.session_open_utc(dt.date(2026, 3, 5))


def test_weekend_event_maps_to_monday():
    anchor, basis = mcal.anchor(eastern(2026, 3, 7, 12, 0))  # Saturday
    assert basis == "next_open"
    assert anchor == mcal.session_open_utc(dt.date(2026, 3, 9))


def test_holiday_eve_evening_skips_the_holiday():
    # Evening of Dec 24 2026 -> next session is Dec 28 (25th holiday, 26/27 weekend).
    anchor, basis = mcal.anchor(eastern(2026, 12, 24, 19, 0))
    assert basis == "next_open"
    assert anchor == mcal.session_open_utc(dt.date(2026, 12, 28))


def test_trading_days_after_skips_weekends():
    assert mcal.trading_days_after(dt.date(2026, 3, 5), 1) == dt.date(2026, 3, 6)
    assert mcal.trading_days_after(dt.date(2026, 3, 6), 1) == dt.date(2026, 3, 9)
    assert mcal.trading_days_after(dt.date(2026, 3, 4), 5) == dt.date(2026, 3, 11)


def test_previous_trading_day_skips_holidays():
    assert mcal.previous_trading_day(dt.date(2026, 1, 2)) == dt.date(2025, 12, 31)


def test_everything_is_stored_in_utc():
    """Sanity: session boundaries round-trip through UTC without drifting."""
    day = dt.date(2026, 7, 15)
    opened = mcal.session_open_utc(day)
    assert opened.tzinfo is not None
    assert opened.astimezone(mcal.EASTERN).time() == mcal.REGULAR_OPEN
