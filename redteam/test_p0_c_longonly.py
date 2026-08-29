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

def test_unresolved_prior_sell_plus_proposed_sell_is_now_aggregated():
    """RT-01 FIXED. A prior SELL still live at the broker now reserves its
    remaining quantity, so a second full-position SELL is rejected. An
    UNKNOWN order with no recoverable size fails closed entirely."""
    sized = UnresolvedOrder(
        proposal_id="prior", symbol="SGOV", side=Side.SELL,
        client_order_id="opaca-deadbeef", state=OrderState.UNKNOWN,
        quantity=Decimal("100"), filled_quantity=Decimal("0"),
    )
    ctx = make_context(investment_policy=NO_CONC, positions=(POS,), unresolved_orders=(sized,))
    d = evaluate(_sell("p8", [("SGOV", "100")]), ctx)
    assert not d.result_for(CheckId.CHECK_16).passed
    assert not d.passed

    unsized = UnresolvedOrder(
        proposal_id="prior", symbol="SGOV", side=Side.SELL,
        client_order_id="opaca-deadbeef", state=OrderState.UNKNOWN,
    )
    ctx2 = make_context(investment_policy=NO_CONC, positions=(POS,), unresolved_orders=(unsized,))
    r = evaluate(_sell("p8b", [("SGOV", "1")]), ctx2).result_for(CheckId.CHECK_16)
    assert not r.passed and "undeterminable" in r.detail


def test_unresolved_prior_buy_opposing_is_caught():
    """Control: the opposing-side case IS caught by CHECK-10."""
    unresolved = UnresolvedOrder(
        proposal_id="prior", symbol="SGOV", side=Side.BUY,
        client_order_id="opaca-deadbeef", state=OrderState.NEW,
    )
    ctx = make_context(investment_policy=NO_CONC, positions=(POS,), unresolved_orders=(unresolved,))
    d = evaluate(_sell("p9", [("SGOV", "50")]), ctx)
    assert not d.result_for(CheckId.CHECK_10).passed


def test_two_concurrent_proposals_against_one_snapshot_remain_an_orchestration_gap():
    """Still true after RT-01, and correctly documented as such: a stateless
    engine cannot serialise two simultaneous callers. The execution layer
    must do evaluate -> reserve -> persist atomically."""
    import opaca.policy.engine as engine_mod
    ctx = make_context(investment_policy=NO_CONC, positions=(POS,))
    a = evaluate(_sell("pa", [("SGOV", "60")]), ctx)
    b = evaluate(_sell("pb", [("SGOV", "60")]), ctx)
    assert a.result_for(CheckId.CHECK_16).passed
    assert b.result_for(CheckId.CHECK_16).passed
    doc = engine_mod.__doc__ or ""
    assert "ATOMIC" in doc and "single-writer" in doc, "gap must stay documented"


