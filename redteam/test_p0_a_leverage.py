"""P0-A: cash / leverage isolation."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from helpers import (
    DEFAULT_NOW, decide, evaluate, make_context, make_order, make_proposal,
)
from opaca.domain.models import BrokerCashState, CheckId, Side

EXTREMES = [
    ("100000", "100000", "100000", "1"),
    ("100000", "400000", "100000", "4"),
    ("100000", "400000", "400000", "4"),
    ("100000", "999999999999", "999999999999", "9999"),
    ("100000", "0", "0", "0"),
    ("100000", "100000.01", "100000.01", "1"),
    ("100000", "0.00000001", "0", "1"),
]


def _broker(cash, bp, nmbp, mult):
    return BrokerCashState(
        cash=Decimal(cash), buying_power=Decimal(bp),
        non_marginable_buying_power=Decimal(nmbp),
        multiplier=Decimal(mult), as_of=DEFAULT_NOW,
    )


def _proposal(qty="50"):
    return make_proposal("p-lev", [
        make_order("p-lev", 0, "SGOV", Side.BUY, qty, "100.69")
    ])


def test_policy_outcome_invariant_across_all_leverage_fields():
    """Holding cash and every real policy input constant, no leverage-derived
    field may change the funding/authority outcome."""
    proposal = _proposal()
    baseline_decision = None
    baseline_authority = None
    for cash, bp, nmbp, mult in EXTREMES:
        ctx = make_context(cash=cash, broker=_broker(cash, bp, nmbp, mult))
        decision = evaluate(proposal, ctx)
        authority = decide(proposal, ctx)
        if baseline_decision is None:
            baseline_decision, baseline_authority = decision, authority
        assert decision == baseline_decision, f"policy changed for bp={bp} nmbp={nmbp} mult={mult}"
        assert authority.result == baseline_authority.result


def test_non_marginable_buying_power_above_cash_grants_no_funding():
    """cash=100000 but non_marginable_buying_power=250000: deployable amount
    must still be bounded by reconciled cash minus protected liquidity."""
    ctx = make_context(cash="100000", broker=_broker("100000", "400000", "250000", "4"))
    # investable = 100000 - reserve(40000) - payroll(24000) - suppliers(14000) = 22000
    over = make_proposal("p-over", [
        make_order("p-over", 0, "SGOV", Side.BUY, "230", "100.69")  # 23158.70
    ])
    decision = evaluate(over, ctx)
    assert not decision.passed
    assert not decision.result_for(CheckId.CHECK_01).passed
    assert not decision.result_for(CheckId.CHECK_06).passed


def test_deployment_above_cash_but_below_buying_power_is_rejected():
    """Classic 4x leverage attack: 300k buy with 100k cash / 400k buying power."""
    ctx = make_context(cash="100000", broker=_broker("100000", "400000", "100000", "4"))
    prop = make_proposal("p-margin", [
        make_order("p-margin", 0, "SGOV", Side.BUY, "2980", "100.69")  # 300,056.20
    ])
    decision = evaluate(prop, ctx)
    assert not decision.passed
    for cid in (CheckId.CHECK_01, CheckId.CHECK_06, CheckId.CHECK_11):
        assert not decision.result_for(cid).passed, f"{cid} failed to reject leveraged buy"


def test_check11_boundary_is_settled_cash_not_buying_power():
    """CHECK-11 must bind at reconciled settled cash exactly."""
    ctx = make_context(cash="100000", operating_reserve=Decimal("0"), obligations=(),
                       broker=_broker("100000", "400000", "400000", "4"))
    at_cash = make_proposal("p-at", [
        make_order("p-at", 0, "SGOV", Side.BUY, "993", "100.69")  # 99,985.17
    ])
    assert evaluate(at_cash, ctx).result_for(CheckId.CHECK_11).passed
    over_cash = make_proposal("p-ov", [
        make_order("p-ov", 0, "SGOV", Side.BUY, "994", "100.69")  # 100,085.86
    ])
    assert not evaluate(over_cash, ctx).result_for(CheckId.CHECK_11).passed
