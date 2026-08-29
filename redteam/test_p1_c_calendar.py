"""P1-C: calendar / settlement."""
from __future__ import annotations

import signal
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from helpers import evaluate, make_context, make_order, make_proposal
from opaca.calendar.us_trading_calendar import (
    US_TRADING_CALENDAR, CalendarError, StaticTradingCalendar, TradingSession,
    USTradingCalendar,
)
from opaca.domain.models import CheckId, Side


# ---------------------------------------------------------- required cases
def test_friday_2026_08_28_settles_monday_2026_08_31():
    assert US_TRADING_CALENDAR.settlement_date(date(2026, 8, 28)) == date(2026, 8, 31)


def test_friday_2026_09_04_settles_tuesday_2026_09_08_over_labor_day():
    assert US_TRADING_CALENDAR.is_trading_day(date(2026, 9, 7)) is False
    assert US_TRADING_CALENDAR.settlement_date(date(2026, 9, 4)) == date(2026, 9, 8)


def test_weekend_trade_date_rolls_forward():
    assert US_TRADING_CALENDAR.settlement_date(date(2026, 8, 29)) == date(2026, 8, 31)
    assert US_TRADING_CALENDAR.settlement_date(date(2026, 8, 30)) == date(2026, 8, 31)


def test_settlement_is_strictly_after_trade_date():
    day = date(2026, 1, 1)
    while day < date(2026, 12, 31):
        assert US_TRADING_CALENDAR.settlement_date(day) > day
        day += timedelta(days=1)


def test_early_close_session_is_1300():
    s = US_TRADING_CALENDAR.session(date(2026, 11, 27))
    assert s is not None and s.close_time == time(13, 0)


def test_negative_and_zero_cycles_fail_closed():
    with pytest.raises(CalendarError):
        US_TRADING_CALENDAR.settlement_date(date(2026, 9, 1), cycle=0)
    with pytest.raises(CalendarError):
        US_TRADING_CALENDAR.add_trading_days(date(2026, 9, 1), -1)


# ---------------------------------------------------------- ATTACKS
def test_dates_outside_the_supported_window_now_fail_closed():
    """RT-03 FIXED. The weekday rule is no longer extrapolated past the
    verified holiday table; out-of-range dates raise CalendarError."""
    from opaca.calendar.us_trading_calendar import SUPPORTED_RANGE_END, SUPPORTED_RANGE_START
    jul4_2028 = date(2028, 7, 4)
    assert jul4_2028.weekday() == 1
    for day in (jul4_2028, date(2024, 6, 3),
                SUPPORTED_RANGE_END + timedelta(days=1),
                SUPPORTED_RANGE_START - timedelta(days=1)):
        with pytest.raises(CalendarError):
            US_TRADING_CALENDAR.is_trading_day(day)
        with pytest.raises(CalendarError):
            US_TRADING_CALENDAR.session(day)
    with pytest.raises(CalendarError):
        US_TRADING_CALENDAR.settlement_date(date(2027, 12, 31))


def test_christmas_2028_now_fails_closed_too():
    with pytest.raises(CalendarError):
        US_TRADING_CALENDAR.is_trading_day(date(2028, 12, 25))


def test_static_calendar_past_last_session_now_raises_CalendarError_fast():
    """RT-04 FIXED. Bounded by last_supported_date(): a CalendarError, the
    module's declared fail-closed type, raised immediately."""
    import time as _time
    sessions = [
        TradingSession(date(2026, 10, 9), time(9, 30), time(16, 0)),
        TradingSession(date(2026, 10, 12), time(9, 30), time(16, 0)),
    ]
    cal = StaticTradingCalendar(sessions)
    start = _time.time()
    with pytest.raises(CalendarError):
        cal.settlement_date(date(2026, 10, 12))
    assert _time.time() - start < 1.0, "must not scan toward date.max"
    with pytest.raises(CalendarError):
        StaticTradingCalendar([]).next_trading_day(date(2026, 9, 1))


def test_calendar_exhaustion_is_caught_by_the_engine_and_fails_checks_closed():
    """RT-04 FIXED. No exception escapes evaluate(); CHECK-02 and CHECK-12
    fail closed instead."""
    from opaca.domain.models import Position
    sessions = [TradingSession(date(2026, 9, 1), time(9, 30), time(16, 0))]
    cal = StaticTradingCalendar(sessions)
    pos = Position("SGOV", Decimal("100"), Decimal("100"), Decimal("10069"))
    ctx = make_context(positions=(pos,), calendar=cal)
    prop = make_proposal("c9", [make_order("c9", 0, "SGOV", Side.SELL, "10", "100.69")])
    d = evaluate(prop, ctx)
    assert not d.result_for(CheckId.CHECK_02).passed
    assert not d.result_for(CheckId.CHECK_12).passed
    assert not d.passed


def test_naive_datetime_rejected_at_the_boundary():
    with pytest.raises(ValueError):
        make_context(now=datetime(2026, 9, 1, 14, 30))


def test_non_utc_aware_datetime_rejected():
    tz = timezone(timedelta(hours=8))
    with pytest.raises(ValueError):
        make_context(now=datetime(2026, 9, 1, 22, 30, tzinfo=tz))


def test_check15_fails_closed_on_non_trading_day():
    """Saturday: no session -> CHECK-15 must fail closed."""
    sat = datetime(2026, 9, 5, 14, 30, tzinfo=timezone.utc)
    ctx = make_context(now=sat, seed_date=date(2026, 9, 5))
    prop = make_proposal("c1", [make_order("c1", 0, "SGOV", Side.BUY, "1", "100.69")])
    r = evaluate(prop, ctx).result_for(CheckId.CHECK_15)
    assert not r.passed and "fail closed" in r.detail


def test_check15_fails_closed_on_labor_day():
    labor = datetime(2026, 9, 7, 14, 30, tzinfo=timezone.utc)
    ctx = make_context(now=labor, seed_date=date(2026, 9, 7))
    prop = make_proposal("c2", [make_order("c2", 0, "SGOV", Side.BUY, "1", "100.69")])
    assert not evaluate(prop, ctx).result_for(CheckId.CHECK_15).passed


def test_check15_blackout_window_boundaries_dst_aware():
    """2026-09-01 is EDT (UTC-4); 16:00 local close = 20:00 UTC."""
    prop = make_proposal("c3", [make_order("c3", 0, "SGOV", Side.BUY, "1", "100.69")])
    inside = datetime(2026, 9, 1, 19, 50, tzinfo=timezone.utc)   # 15:50 EDT
    outside = datetime(2026, 9, 1, 19, 44, tzinfo=timezone.utc)  # 15:44 EDT
    assert not evaluate(prop, make_context(now=inside)).result_for(CheckId.CHECK_15).passed
    assert evaluate(prop, make_context(now=outside)).result_for(CheckId.CHECK_15).passed


def test_check15_winter_dst_transition_uses_est_offset():
    """2026-12-01 is EST (UTC-5); 16:00 local close = 21:00 UTC."""
    prop = make_proposal("c4", [make_order("c4", 0, "SGOV", Side.BUY, "1", "100.69")])
    inside = datetime(2026, 12, 1, 20, 50, tzinfo=timezone.utc)  # 15:50 EST
    outside = datetime(2026, 12, 1, 19, 50, tzinfo=timezone.utc) # 14:50 EST
    ctx_i = make_context(now=inside, seed_date=date(2026, 12, 1))
    ctx_o = make_context(now=outside, seed_date=date(2026, 12, 1))
    assert not evaluate(prop, ctx_i).result_for(CheckId.CHECK_15).passed
    assert evaluate(prop, ctx_o).result_for(CheckId.CHECK_15).passed


def test_check15_after_close_is_not_blacked_out():
    """Documented behaviour: after the close the window has passed."""
    prop = make_proposal("c5", [make_order("c5", 0, "SGOV", Side.BUY, "1", "100.69")])
    after = datetime(2026, 9, 1, 20, 30, tzinfo=timezone.utc)    # 16:30 EDT
    assert evaluate(prop, make_context(now=after)).result_for(CheckId.CHECK_15).passed


def test_early_close_day_blackout_uses_1300_not_1600():
    """2026-11-27 early close 13:00 EST = 18:00 UTC."""
    prop = make_proposal("c6", [make_order("c6", 0, "SGOV", Side.BUY, "1", "100.69")])
    inside = datetime(2026, 11, 27, 17, 50, tzinfo=timezone.utc)  # 12:50 EST
    ctx = make_context(now=inside, seed_date=date(2026, 11, 27))
    assert not evaluate(prop, ctx).result_for(CheckId.CHECK_15).passed
