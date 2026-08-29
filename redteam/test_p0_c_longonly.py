"""P0-C: long-only / oversell."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from helpers import evaluate, make_context, make_order, make_proposal
from opaca.domain.models import (
    CheckId, InvestmentPolicy, OrderState, Position, PrecloseBlackoutConfig,
    Side, UnresolvedOrder,
)

# Concentration relaxed so P0-C isolates CHECK-16 (see finding F-01).
NO_CONC = InvestmentPolicy(
    permitted_symbols=frozenset({"SGOV", "BIL", "SHV"}),
    concentration_max_fraction=Decimal("1"),
    min_trade_notional=Decimal("1.00"),
    preclose_blackout=PrecloseBlackoutConfig(enabled=True, minutes_before_close=15),
)

POS = Position(symbol="SGOV", quantity=Decimal("100"),
               quantity_available=Decimal("100"), market_value=Decimal("10069"))


def _sell(pid, legs):
    return make_proposal(pid, [
        make_order(pid, i, sym, Side.SELL, qty, "100.69") for i, (sym, qty) in enumerate(legs)
    ])


def test_sell_exceeding_position_rejected():
    ctx = make_context(investment_policy=NO_CONC, positions=(POS,))
    d = evaluate(_sell("p1", [("SGOV", "101")]), ctx)
    assert not d.result_for(CheckId.CHECK_16).passed


def test_sell_exact_equality_allowed():
    ctx = make_context(investment_policy=NO_CONC, positions=(POS,))
    assert evaluate(_sell("p2", [("SGOV", "100")]), ctx).result_for(CheckId.CHECK_16).passed


def test_fractional_oversell_by_one_quantum_rejected():
    ctx = make_context(investment_policy=NO_CONC, positions=(POS,))
    d = evaluate(_sell("p3", [("SGOV", "100.000000001")]), ctx)
    assert not d.result_for(CheckId.CHECK_16).passed


def test_sell_with_no_position_at_all_rejected():
    ctx = make_context(investment_policy=NO_CONC, positions=())
    d = evaluate(_sell("p4", [("SGOV", "1")]), ctx)
    assert not d.result_for(CheckId.CHECK_16).passed


def test_multiple_sell_legs_same_symbol_aggregate_enforced():
    """Each leg individually <= position, aggregate > position."""
    ctx = make_context(investment_policy=NO_CONC, positions=(POS,))
    d = evaluate(_sell("p5", [("SGOV", "60"), ("SGOV", "60")]), ctx)
    assert not d.result_for(CheckId.CHECK_16).passed, "aggregate oversell not caught"


def test_quantity_available_below_quantity_is_the_binding_limit():
    pos = Position(symbol="SGOV", quantity=Decimal("100"),
                   quantity_available=Decimal("40"), market_value=Decimal("10069"))
    ctx = make_context(investment_policy=NO_CONC, positions=(pos,))
    assert not evaluate(_sell("p6", [("SGOV", "50")]), ctx).result_for(CheckId.CHECK_16).passed
    assert evaluate(_sell("p7", [("SGOV", "40")]), ctx).result_for(CheckId.CHECK_16).passed


# ---------------------------------------------------------------- cross-proposal

def test_unresolved_prior_sell_plus_proposed_sell_is_NOT_aggregated():
    """ATTACK: a prior SELL is still live at the broker in UNKNOWN state, so
    the broker has not yet reserved the shares and quantity_available still
    reads the full position. A second full-position SELL is proposed."""
    unresolved = UnresolvedOrder(
        proposal_id="prior", symbol="SGOV", side=Side.SELL,
        client_order_id="opaca-deadbeef", state=OrderState.UNKNOWN,
    )
    ctx = make_context(investment_policy=NO_CONC, positions=(POS,), unresolved_orders=(unresolved,))
    d = evaluate(_sell("p8", [("SGOV", "100")]), ctx)
    # CHECK-10 only looks for OPPOSING sides, so a same-side sell is invisible
    assert d.result_for(CheckId.CHECK_10).passed
    assert d.result_for(CheckId.CHECK_16).passed
    assert d.passed, "documents the uncovered cross-proposal invariant"


def test_unresolved_prior_buy_opposing_is_caught():
    """Control: the opposing-side case IS caught by CHECK-10."""
    unresolved = UnresolvedOrder(
        proposal_id="prior", symbol="SGOV", side=Side.BUY,
        client_order_id="opaca-deadbeef", state=OrderState.NEW,
    )
    ctx = make_context(investment_policy=NO_CONC, positions=(POS,), unresolved_orders=(unresolved,))
    d = evaluate(_sell("p9", [("SGOV", "50")]), ctx)
    assert not d.result_for(CheckId.CHECK_10).passed


def test_two_concurrent_proposals_each_safe_alone_aggregate_oversells():
    """ATTACK: two logically concurrent proposals, each evaluated against the
    same reconciled snapshot, each <= position, together > position."""
    ctx = make_context(investment_policy=NO_CONC, positions=(POS,))
    a = evaluate(_sell("pa", [("SGOV", "60")]), ctx)
    b = evaluate(_sell("pb", [("SGOV", "60")]), ctx)
    assert a.result_for(CheckId.CHECK_16).passed
    assert b.result_for(CheckId.CHECK_16).passed
    assert a.passed and b.passed, "no cross-proposal reservation exists in this phase"
