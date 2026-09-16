"""US equity market calendar.

Deliberately hand-rolled rather than pulling in `pandas_market_calendars` /
`exchange_calendars`: we need a handful of rules, they are stable, and a 150-line
module with tests beats a 50 MB dependency tree on a 4 GB VPS.

All inputs and outputs are timezone-aware UTC datetimes. Internally we convert
to US/Eastern because that is what defines a session.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

REGULAR_OPEN = dt.time(9, 30)
REGULAR_CLOSE = dt.time(16, 0)
PREMARKET_OPEN = dt.time(4, 0)
AFTERHOURS_CLOSE = dt.time(20, 0)
EARLY_CLOSE = dt.time(13, 0)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """n-th `weekday` (Mon=0) of a month; n=-1 means the last one."""
    if n > 0:
        first = dt.date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + dt.timedelta(days=offset + 7 * (n - 1))
    last_day = (dt.date(year, month % 12 + 1, 1) if month < 12 else dt.date(year + 1, 1, 1)) - dt.timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - dt.timedelta(days=offset)


def _easter(year: int) -> dt.date:
    """Anonymous Gregorian algorithm -- needed only for Good Friday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lu = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lu) // 451
    month, day = divmod(h + lu - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def _observed(day: dt.date) -> dt.date:
    """Saturday holidays observe Friday, Sunday holidays observe Monday."""
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


def market_holidays(year: int) -> set[dt.date]:
    """Full-day NYSE closures for a calendar year."""
    days = {
        _observed(dt.date(year, 1, 1)),                 # New Year's Day
        _nth_weekday(year, 1, 0, 3),                    # MLK Jr Day
        _nth_weekday(year, 2, 0, 3),                    # Washington's Birthday
        _easter(year) - dt.timedelta(days=2),           # Good Friday
        _nth_weekday(year, 5, 0, -1),                   # Memorial Day
        _nth_weekday(year, 9, 0, 1),                    # Labor Day
        _nth_weekday(year, 11, 3, 4),                   # Thanksgiving
        _observed(dt.date(year, 12, 25)),               # Christmas
        _observed(dt.date(year, 7, 4)),                 # Independence Day
    }
    if year >= 2021:
        days.add(_observed(dt.date(year, 6, 19)))       # Juneteenth
    return days


def early_close_days(year: int) -> set[dt.date]:
    """Sessions that close at 13:00 ET."""
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    days = {thanksgiving + dt.timedelta(days=1)}
    july3 = dt.date(year, 7, 3)
    if july3.weekday() < 5 and _observed(dt.date(year, 7, 4)) == dt.date(year, 7, 4):
        days.add(july3)
    dec24 = dt.date(year, 12, 24)
    if dec24.weekday() < 5:
        days.add(dec24)
    return days


def is_trading_day(day: dt.date) -> bool:
    return day.weekday() < 5 and day not in market_holidays(day.year)


def session_close(day: dt.date) -> dt.time:
    return EARLY_CLOSE if day in early_close_days(day.year) else REGULAR_CLOSE


def next_trading_day(day: dt.date, *, inclusive: bool = False) -> dt.date:
    candidate = day if inclusive else day + dt.timedelta(days=1)
    for _ in range(15):
        if is_trading_day(candidate):
            return candidate
        candidate += dt.timedelta(days=1)
    raise ValueError(f"no trading day found within 15 days of {day}")


def previous_trading_day(day: dt.date, *, inclusive: bool = False) -> dt.date:
    candidate = day if inclusive else day - dt.timedelta(days=1)
    for _ in range(15):
        if is_trading_day(candidate):
            return candidate
        candidate -= dt.timedelta(days=1)
    raise ValueError(f"no trading day found within 15 days before {day}")


def classify(moment: dt.datetime) -> str:
    """One of: regular, premarket, afterhours, closed.

    `closed` covers weekends, holidays and the overnight gap.
    """
    if moment.tzinfo is None:
        raise ValueError("classify() requires a timezone-aware datetime")
    local = moment.astimezone(EASTERN)
    day, clock = local.date(), local.time()
    if not is_trading_day(day):
        return "closed"
    close = session_close(day)
    if REGULAR_OPEN <= clock < close:
        return "regular"
    if PREMARKET_OPEN <= clock < REGULAR_OPEN:
        return "premarket"
    if close <= clock < AFTERHOURS_CLOSE:
        return "afterhours"
    return "closed"


def session_open_utc(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, REGULAR_OPEN, tzinfo=EASTERN).astimezone(UTC)


def session_close_utc(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, session_close(day), tzinfo=EASTERN).astimezone(UTC)


def anchor(moment: dt.datetime) -> tuple[dt.datetime, str]:
    """Map an event time to the moment the market can first react to it.

    Returns ``(anchor_time_utc, basis)`` where `basis` is one of:

    * ``intraday``    -- the event landed inside a regular session; the anchor is
      the event time itself and intraday returns are meaningful.
    * ``next_open``   -- the event landed outside regular hours; the anchor is the
      next regular session open. A 2 a.m. post has no 5-minute return, and this
      is how we say so.

    The basis string is stored alongside every computed return and shown in the
    UI, so a "1d return" is never silently a different thing for two events.
    """
    if moment.tzinfo is None:
        raise ValueError("anchor() requires a timezone-aware datetime")
    if classify(moment) == "regular":
        return moment.astimezone(UTC), "intraday"

    local = moment.astimezone(EASTERN)
    day, clock = local.date(), local.time()
    if is_trading_day(day) and clock < REGULAR_OPEN:
        target = day
    else:
        target = next_trading_day(day)
    return session_open_utc(target), "next_open"


def trading_days_after(day: dt.date, n: int) -> dt.date:
    """The n-th trading day strictly after `day`."""
    current = day
    for _ in range(n):
        current = next_trading_day(current)
    return current
