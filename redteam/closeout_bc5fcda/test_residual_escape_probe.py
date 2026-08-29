"""Residual sweep: is any exception still able to escape evaluate()/decide()?

Both P1s at 5d33a05 were of the class 'an exception escapes instead of
becoming a failed check'. These probes look for what is left of that class at
the target commit, and pin the answers.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from helpers import (  # type: ignore[import-not-found]
    DEFAULT_NOW,
    ENGINE,
    decide,
    evaluate,
    make_context,
    make_order,
    make_proposal,
)
from opaca.calendar.us_trading_calendar import USTradingCalendar
from opaca.domain.models import (
    AuthorityResult,
    Obligation,
    Position,
    SettlementEvent,
    Side,
)
from opaca.domain.money import MoneyError
from opaca.policy.partial_fill import assess_partial_fill_safety
from opaca.treasury.liquidity import LedgerInconsistencyError

SGOV = "SGOV"
DEFAULT = {"BIL": Decimal("92"), "SHV": Decimal("110")}


# --- domain boundaries reject before evaluate() is ever entered -------------


def test_oversized_quantity_is_rejected_at_the_order_boundary() -> None:
    with pytest.raises(MoneyError):
        make_order("p", 0, SGOV, Side.BUY, Decimal("1e20"), Decimal("100"))


def test_oversized_market_value_is_rejected_at_the_position_boundary() -> None:
    with pytest.raises(MoneyError):
        Position(SGOV, Decimal("1e13"), Decimal("1e13"), Decimal("1e27"))


# --- RESIDUAL R-1: notional overflow escapes evaluate() ---------------------


def test_residual_leg_notional_overflow_escapes_evaluate() -> None:
    """Characterisation. quantity 1e13 and price 1e13 each pass their own
    validator; their product hits MAGNITUDE_LIMIT and MoneyError escapes
    evaluate() instead of becoming a failed check. Pre-existing at 5d33a05,
    unchanged here — not a regression."""
    order = make_order("p", 0, SGOV, Side.BUY, Decimal("1e13"), Decimal("1e13"))
    ctx = make_context(cash="100000")
    with pytest.raises(MoneyError):
        evaluate(make_proposal("p", [order]), ctx)


def test_residual_holdings_value_overflow_escapes_evaluate() -> None:
    """Same class, via CHECK-04's own qty x price multiplication."""
    q = Decimal("1e13")
    ctx = make_context(
        cash="100000",
        positions=[Position(SGOV, q, q, Decimal("0"))],
        prices={SGOV: Decimal("1e13"), **DEFAULT},
    )
    order = make_order("p", 0, SGOV, Side.BUY, Decimal("1"), Decimal("100.69"))
    with pytest.raises(MoneyError):
        evaluate(make_proposal("p", [order]), ctx)


def test_one_order_of_magnitude_below_is_a_normal_failed_decision() -> None:
    """The residual is a magnitude cliff, not a general breakage."""
    order = make_order("p", 0, SGOV, Side.BUY, Decimal("1e12"), Decimal("1e12"))
    decision = evaluate(make_proposal("p", [order]), make_context(cash="100000"))
    assert decision.passed is False


# --- RESIDUAL R-2: partial-fill still calls compute_liquidity unguarded ----


def test_residual_assess_partial_fill_safety_raises_on_an_inconsistent_ledger() -> None:
    """Characterisation. Unreachable through decide(), which short-circuits on
    the failed base decision (asserted below), but a direct caller gets the raw
    exception."""
    q = Decimal("100")
    ctx = make_context(
        cash="100000",
        settlement_events=[
            SettlementEvent("e1", SGOV, date(2026, 9, 1), date(2026, 9, 2), Decimal("200000"))
        ],
        positions=[Position(SGOV, q, q, q * Decimal("100.69"))],
    )
    proposal = make_proposal(
        "p", [make_order("p", 0, SGOV, Side.SELL, Decimal("10"), Decimal("100.69"))]
    )
    with pytest.raises(LedgerInconsistencyError):
        assess_partial_fill_safety(proposal, ctx, ENGINE)
    # the composed entry point is safe
    assert decide(proposal, ctx).result is AuthorityResult.REJECT


# --- things that must NOT raise --------------------------------------------


def test_out_of_range_now_fails_closed_without_raising() -> None:
    ctx = make_context(cash="100000", now=DEFAULT_NOW.replace(year=2030))
    order = make_order("p", 0, SGOV, Side.BUY, Decimal("1"), Decimal("100.69"))
    decision = evaluate(make_proposal("p", [order]), ctx)
    assert decision.passed is False
    assert decide(make_proposal("p", [order]), ctx).result is AuthorityResult.REJECT


def test_single_day_calendar_fails_closed_without_raising() -> None:
    tiny = USTradingCalendar(supported_start=date(2026, 9, 1), supported_end=date(2026, 9, 1))
    q = Decimal("10")
    ctx = make_context(
        cash="100000", calendar=tiny, positions=[Position(SGOV, q, q, Decimal("1006.90"))]
    )
    order = make_order("p", 0, SGOV, Side.SELL, Decimal("1"), Decimal("100.69"))
    decision = evaluate(make_proposal("p", [order]), ctx)
    assert decision.passed is False


def test_obligation_far_outside_the_calendar_range_does_not_raise() -> None:
    ctx = make_context(
        cash="100000", obligations=[Obligation("o1", "far", Decimal("1000"), date(2099, 1, 1))]
    )
    order = make_order("p", 0, SGOV, Side.BUY, Decimal("1"), Decimal("100.69"))
    assert evaluate(make_proposal("p", [order]), ctx).passed in (True, False)


def test_context_construction_failure_leaves_the_engine_usable() -> None:
    with pytest.raises(MoneyError):
        make_context(prices={SGOV: Decimal("0"), **DEFAULT})
    ctx = make_context(cash="100000")
    order = make_order("p", 0, SGOV, Side.BUY, Decimal("10"), Decimal("100.69"))
    assert evaluate(make_proposal("p", [order]), ctx).passed is True
