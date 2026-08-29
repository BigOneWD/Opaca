"""P2-b retest: the supported-range guard must apply to the INPUT of
settlement_date / next_trading_day / add_trading_days, not only to the
candidate days walked."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from opaca.calendar.us_trading_calendar import (
    SUPPORTED_RANGE_END,
    SUPPORTED_RANGE_START,
    US_TRADING_CALENDAR,
    CalendarError,
)

CAL = US_TRADING_CALENDAR
DAY = timedelta(days=1)


# --- the exact one-day hole reported at 5d33a05 ----------------------------


def test_settlement_date_one_day_below_range_start_fails_closed() -> None:
    """5d33a05 returned 2025-01-02 for this input instead of raising."""
    with pytest.raises(CalendarError):
        CAL.settlement_date(SUPPORTED_RANGE_START - DAY)


def test_the_literal_reported_case() -> None:
    with pytest.raises(CalendarError):
        CAL.settlement_date(date(2024, 12, 31))


@pytest.mark.parametrize(
    "day",
    [
        SUPPORTED_RANGE_START - DAY,
        SUPPORTED_RANGE_START - timedelta(days=2),
        SUPPORTED_RANGE_START - timedelta(days=400),
        SUPPORTED_RANGE_END + DAY,
        SUPPORTED_RANGE_END + timedelta(days=400),
        date.min,
        date.max,
    ],
)
@pytest.mark.parametrize("fn", ["settlement_date", "next_trading_day", "add_trading_days"])
def test_every_walking_api_rejects_out_of_range_input(fn: str, day: date) -> None:
    with pytest.raises(CalendarError):
        if fn == "add_trading_days":
            CAL.add_trading_days(day, 1)
        else:
            getattr(CAL, fn)(day)


def test_add_trading_days_zero_count_still_validates_input() -> None:
    """count=0 short-circuits the walk, so only an input guard can catch it."""
    with pytest.raises(CalendarError):
        CAL.add_trading_days(SUPPORTED_RANGE_START - DAY, 0)


def test_is_trading_day_and_session_still_guard() -> None:
    with pytest.raises(CalendarError):
        CAL.is_trading_day(SUPPORTED_RANGE_START - DAY)
    with pytest.raises(CalendarError):
        CAL.session(SUPPORTED_RANGE_END + DAY)


def test_trading_days_between_fails_closed_on_out_of_range_bounds() -> None:
    with pytest.raises(CalendarError):
        CAL.trading_days_between(SUPPORTED_RANGE_START - DAY, SUPPORTED_RANGE_START + DAY)
    with pytest.raises(CalendarError):
        CAL.trading_days_between(SUPPORTED_RANGE_END - DAY, SUPPORTED_RANGE_END + DAY)


# --- in-range behaviour must be unchanged ----------------------------------


def test_range_boundaries_themselves_are_accepted() -> None:
    assert CAL.is_trading_day(SUPPORTED_RANGE_START) in (True, False)
    assert CAL.is_trading_day(SUPPORTED_RANGE_END) in (True, False)
    assert CAL.settlement_date(date(2026, 9, 1)) == date(2026, 9, 2)


def test_t_plus_one_over_a_weekend_is_unchanged() -> None:
    # Fri 2026-09-04 -> Mon 2026-09-07 is Labor Day -> Tue 2026-09-08
    assert CAL.settlement_date(date(2026, 9, 4)) == date(2026, 9, 8)


def test_exhaustion_at_the_end_of_the_range_still_fails_closed() -> None:
    with pytest.raises(CalendarError):
        CAL.settlement_date(SUPPORTED_RANGE_END)


def test_negative_cycle_still_rejected() -> None:
    with pytest.raises(CalendarError):
        CAL.settlement_date(date(2026, 9, 1), cycle=0)
