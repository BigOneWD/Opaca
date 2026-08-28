"""US trading-calendar behavior: T+1, weekends, exchange holidays.

Required proofs 13, 14, 15 plus an exact cross-check of the built-in
calendar against the Phase -1 calendar evidence.
"""

from __future__ import annotations

from datetime import date, time

import pytest
from opaca.calendar.us_trading_calendar import (
    EARLY_SESSION_CLOSE,
    US_TRADING_CALENDAR,
    CalendarError,
    StaticTradingCalendar,
    TradingSession,
)

from tests.helpers import phase1_calendar_session_dates

EVIDENCE_WINDOW_START = date(2026, 8, 28)
EVIDENCE_WINDOW_END = date(2026, 10, 12)


class TestTPlusOneSettlement:
    def test_friday_t_plus_1_settles_monday(self) -> None:
        # Required proof 13. 2026-08-28 is a Friday (Phase -1 spike day);
        # 2026-08-31 is a trading Monday (confirmed by calendar evidence).
        assert US_TRADING_CALENDAR.settlement_date(date(2026, 8, 28)) == date(2026, 8, 31)

    def test_weekday_t_plus_1_is_next_weekday(self) -> None:
        # Tue 2026-09-01 -> Wed 2026-09-02 (both sessions in evidence).
        assert US_TRADING_CALENDAR.settlement_date(date(2026, 9, 1)) == date(2026, 9, 2)

    def test_weekend_dates_are_skipped(self) -> None:
        # Required proof 14.
        assert not US_TRADING_CALENDAR.is_trading_day(date(2026, 8, 29))  # Saturday
        assert not US_TRADING_CALENDAR.is_trading_day(date(2026, 8, 30))  # Sunday
        assert US_TRADING_CALENDAR.add_trading_days(date(2026, 8, 28), 1) == date(2026, 8, 31)
        assert US_TRADING_CALENDAR.trading_days_between(date(2026, 8, 28), date(2026, 9, 1)) == (
            date(2026, 8, 28),
            date(2026, 8, 31),
            date(2026, 9, 1),
        )

    def test_labor_day_2026_is_skipped(self) -> None:
        # Required proof 15. 2026-09-07 is US Labor Day; the Phase -1
        # calendar evidence shows sessions jumping 2026-09-04 -> 2026-09-08.
        assert not US_TRADING_CALENDAR.is_trading_day(date(2026, 9, 7))
        assert US_TRADING_CALENDAR.settlement_date(date(2026, 9, 4)) == date(2026, 9, 8)

    def test_add_trading_days_rejects_negative(self) -> None:
        with pytest.raises(CalendarError):
            US_TRADING_CALENDAR.add_trading_days(date(2026, 9, 1), -1)

    def test_settlement_cycle_must_be_positive(self) -> None:
        with pytest.raises(CalendarError):
            US_TRADING_CALENDAR.settlement_date(date(2026, 9, 1), cycle=0)


class TestEvidenceCrossCheck:
    def test_builtin_calendar_matches_phase1_evidence_window_exactly(self) -> None:
        evidence_dates = phase1_calendar_session_dates()
        assert len(evidence_dates) == 31
        derived = US_TRADING_CALENDAR.trading_days_between(
            EVIDENCE_WINDOW_START, EVIDENCE_WINDOW_END
        )
        assert derived == evidence_dates

    def test_evidence_window_only_missing_weekday_is_labor_day(self) -> None:
        from datetime import timedelta

        weekdays = []
        candidate = EVIDENCE_WINDOW_START
        while candidate <= EVIDENCE_WINDOW_END:
            if candidate.weekday() < 5:
                weekdays.append(candidate)
            candidate += timedelta(days=1)
        sessions = set(phase1_calendar_session_dates())
        missing = [d for d in weekdays if d not in sessions]
        assert missing == [date(2026, 9, 7)]

    def test_static_calendar_from_evidence_gives_same_settlement_rolls(self) -> None:
        sessions = [
            TradingSession(d, time(9, 30), time(16, 0)) for d in phase1_calendar_session_dates()
        ]
        static = StaticTradingCalendar(sessions)
        assert static.settlement_date(date(2026, 8, 28)) == date(2026, 8, 31)
        assert static.settlement_date(date(2026, 9, 4)) == date(2026, 9, 8)
        assert not static.is_trading_day(date(2026, 9, 7))

    def test_static_calendar_rejects_duplicate_sessions(self) -> None:
        session = TradingSession(date(2026, 9, 1), time(9, 30), time(16, 0))
        with pytest.raises(CalendarError):
            StaticTradingCalendar([session, session])


class TestSessions:
    def test_regular_session_close_is_1600(self) -> None:
        session = US_TRADING_CALENDAR.session(date(2026, 9, 1))
        assert session is not None
        assert session.close_time == time(16, 0)

    def test_day_after_thanksgiving_2026_is_early_close(self) -> None:
        session = US_TRADING_CALENDAR.session(date(2026, 11, 27))
        assert session is not None
        assert session.close_time == EARLY_SESSION_CLOSE

    def test_non_trading_day_has_no_session(self) -> None:
        assert US_TRADING_CALENDAR.session(date(2026, 9, 7)) is None
        assert US_TRADING_CALENDAR.session(date(2026, 8, 29)) is None
