"""Testable US trading-calendar abstraction.

Settlement arithmetic (SPEC s5, Amendment B) uses the US securities
business-day calendar: weekends and exchange holidays are skipped explicitly.

Two implementations:

* ``USTradingCalendar`` - deterministic rule-based calendar (weekday rule plus
  an explicit NYSE holiday/early-close table for 2025-2027).
* ``StaticTradingCalendar`` - built from an authoritative list of sessions
  (e.g. the Alpaca calendar endpoint, or the sanitized Phase -1 evidence in
  ``spike/evidence/calendar_20260828T133740Z.json``).

The Phase -1 evidence window (2026-08-28 .. 2026-10-12, 31 sessions, only
weekday missing 2026-09-07 / US Labor Day) is asserted against
``US_TRADING_CALENDAR`` in the test suite.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, time, timedelta


class CalendarError(ValueError):
    """Raised for invalid calendar operations."""


DEFAULT_SESSION_OPEN = time(9, 30)
DEFAULT_SESSION_CLOSE = time(16, 0)
EARLY_SESSION_CLOSE = time(13, 0)


@dataclass(frozen=True)
class TradingSession:
    session_date: date
    open_time: time
    close_time: time


class TradingCalendar(ABC):
    """Business-day source for T+1 settlement and blackout arithmetic."""

    @abstractmethod
    def is_trading_day(self, day: date) -> bool:
        """True if ``day`` is a full or early trading session."""

    @abstractmethod
    def session(self, day: date) -> TradingSession | None:
        """Session details for ``day`` or None if not a trading day."""

    def next_trading_day(self, day: date) -> date:
        candidate = day + timedelta(days=1)
        while not self.is_trading_day(candidate):
            candidate += timedelta(days=1)
        return candidate

    def add_trading_days(self, day: date, count: int) -> date:
        if count < 0:
            raise CalendarError("add_trading_days does not support negative counts")
        result = day
        for _ in range(count):
            result = self.next_trading_day(result)
        return result

    def settlement_date(self, trade_date: date, cycle: int = 1) -> date:
        """T+``cycle`` settlement on the business-day calendar."""
        if cycle < 1:
            raise CalendarError("settlement cycle must be >= 1")
        return self.add_trading_days(trade_date, cycle)

    def trading_days_between(self, start: date, end: date) -> tuple[date, ...]:
        if end < start:
            raise CalendarError("end precedes start")
        days: list[date] = []
        candidate = start
        while candidate <= end:
            if self.is_trading_day(candidate):
                days.append(candidate)
            candidate += timedelta(days=1)
        return tuple(days)


class StaticTradingCalendar(TradingCalendar):
    """Calendar defined by an explicit session list (authoritative source)."""

    def __init__(self, sessions: Sequence[TradingSession]):
        by_date: dict[date, TradingSession] = {}
        for session in sessions:
            if session.session_date in by_date:
                raise CalendarError(f"duplicate session for {session.session_date}")
            by_date[session.session_date] = session
        self._sessions = by_date

    def is_trading_day(self, day: date) -> bool:
        return day in self._sessions

    def session(self, day: date) -> TradingSession | None:
        return self._sessions.get(day)


#: NYSE holidays 2025-2027. 2026 is verified against Phase -1 calendar
#: evidence (only missing weekday in 2026-08-28..2026-10-12 is 2026-09-07).
#: One-off closures (e.g. national days of mourning) must be added explicitly
#: when relevant; 2025-01-09 (President Carter) is included for completeness.
NYSE_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2025
        date(2025, 1, 1),  # New Year's Day
        date(2025, 1, 9),  # National Day of Mourning (President Carter)
        date(2025, 1, 20),  # Martin Luther King Jr. Day
        date(2025, 2, 17),  # Washington's Birthday
        date(2025, 4, 18),  # Good Friday
        date(2025, 5, 26),  # Memorial Day
        date(2025, 6, 19),  # Juneteenth
        date(2025, 7, 4),  # Independence Day
        date(2025, 9, 1),  # Labor Day
        date(2025, 11, 27),  # Thanksgiving
        date(2025, 12, 25),  # Christmas
        # 2026
        date(2026, 1, 1),  # New Year's Day
        date(2026, 1, 19),  # Martin Luther King Jr. Day
        date(2026, 2, 16),  # Washington's Birthday
        date(2026, 4, 3),  # Good Friday
        date(2026, 5, 25),  # Memorial Day
        date(2026, 6, 19),  # Juneteenth
        date(2026, 7, 3),  # Independence Day observed (Jul 4 falls on Saturday)
        date(2026, 9, 7),  # Labor Day
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 12, 25),  # Christmas
        # 2027
        date(2027, 1, 1),  # New Year's Day
        date(2027, 1, 18),  # Martin Luther King Jr. Day
        date(2027, 2, 15),  # Washington's Birthday
        date(2027, 3, 26),  # Good Friday
        date(2027, 5, 31),  # Memorial Day
        date(2027, 6, 18),  # Juneteenth observed (Jun 19 falls on Saturday)
        date(2027, 7, 5),  # Independence Day observed (Jul 4 falls on Sunday)
        date(2027, 9, 6),  # Labor Day
        date(2027, 11, 25),  # Thanksgiving
        date(2027, 12, 24),  # Christmas observed (Dec 25 falls on Saturday)
    }
)

#: NYSE 13:00 early closes 2025-2027.
NYSE_EARLY_CLOSES: frozenset[date] = frozenset(
    {
        date(2025, 7, 3),
        date(2025, 11, 28),
        date(2025, 12, 24),
        date(2026, 11, 27),
        date(2026, 12, 24),
        date(2027, 11, 26),
    }
)


class USTradingCalendar(TradingCalendar):
    """Rule-based US calendar: Mon-Fri minus explicit holidays."""

    def __init__(
        self,
        holidays: frozenset[date] = NYSE_HOLIDAYS,
        early_closes: frozenset[date] = NYSE_EARLY_CLOSES,
    ):
        self._holidays = holidays
        self._early_closes = early_closes

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self._holidays

    def session(self, day: date) -> TradingSession | None:
        if not self.is_trading_day(day):
            return None
        close = EARLY_SESSION_CLOSE if day in self._early_closes else DEFAULT_SESSION_CLOSE
        return TradingSession(day, DEFAULT_SESSION_OPEN, close)


US_TRADING_CALENDAR = USTradingCalendar()
