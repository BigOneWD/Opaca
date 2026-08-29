"""P1-B: partial fill safety."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from helpers import ENGINE, evaluate, make_context, make_order, make_proposal
from opaca.domain.models import (
    CheckId, InvestmentPolicy, Obligation, Position, PrecloseBlackoutConfig, Side,
)
from opaca.policy.partial_fill import (
    MAX_ENUMERATED_LEGS, assess_partial_fill_safety,
)

SG = Position("SGOV", Decimal("100"), Decimal("100"), Decimal("10069"))
BI = Position("BIL", Decimal("100"), Decimal("100"), Decimal("9200"))


def _pol(conc="0.70"):
    return InvestmentPolicy(
        permitted_symbols=frozenset({"SGOV", "BIL", "SHV"}),
        concentration_max_fraction=Decimal(conc),
        min_trade_notional=Decimal("1.00"),
        preclose_blackout=PrecloseBlackoutConfig(enabled=True, minutes_before_close=15),
    )


def test_subset_count_is_all_non_empty_subsets_2_and_3_legs():
    ctx = make_context(positions=(SG, BI), investment_policy=_pol("1"))
    two = make_proposal("s2", [make_order("s2", 0, "SGOV", Side.BUY, "1", "100.69"),
                               make_order("s2", 1, "BIL", Side.BUY, "1", "92.00")])
    three = make_proposal("s3", [make_order("s3", 0, "SGOV", Side.BUY, "1", "100.69"),
                                 make_order("s3", 1, "BIL", Side.BUY, "1", "92.00"),
                                 make_order("s3", 2, "SHV", Side.BUY, "1", "110.00")])
    assert assess_partial_fill_safety(two, ctx, ENGINE).subsets_evaluated == 3     # 2^2-1
    assert assess_partial_fill_safety(three, ctx, ENGINE).subsets_evaluated == 7   # 2^3-1


def test_single_leg_fill_creating_concentration_is_caught():
    """Full 2-leg buy is balanced and passes; either leg alone is 100%."""
    ctx = make_context(positions=(), investment_policy=_pol("0.70"))
    prop = make_proposal("s4", [make_order("s4", 0, "SGOV", Side.BUY, "50", "100.69"),
                                make_order("s4", 1, "BIL", Side.BUY, "55", "92.00")])
    assert evaluate(prop, ctx).result_for(CheckId.CHECK_04).passed
    a = assess_partial_fill_safety(prop, ctx, ENGINE)
    assert not a.safe
    assert any("CHECK-04" in v for v in a.violations)


def test_no_buy_proposal_from_an_empty_portfolio_can_ever_be_partial_fill_safe():
    """ATTACK/consequence of F-01: every single-leg subset of a buy proposal
    made from an empty portfolio is 100% concentrated, so partial-fill safety
    is unreachable for ANY concentration limit < 1."""
    ctx = make_context(positions=(), investment_policy=_pol("0.99"))
    for n in (2, 3, 4):
        legs = [make_order("s5", i, ["SGOV", "BIL", "SHV"][i % 3], Side.BUY, "10",
                           ["100.69", "92.00", "110.00"][i % 3]) for i in range(n)]
        prop = make_proposal("s5", legs)
        assert not assess_partial_fill_safety(prop, ctx, ENGINE).safe


def test_mixed_notional_sizes_dominant_leg_alone():
    ctx = make_context(positions=(), investment_policy=_pol("0.70"))
    prop = make_proposal("s6", [make_order("s6", 0, "SGOV", Side.BUY, "99", "100.69"),
                                make_order("s6", 1, "BIL", Side.BUY, "1", "92.00")])
    a = assess_partial_fill_safety(prop, ctx, ENGINE)
    assert not a.safe


def test_sell_subsets_are_checked_for_obligation_coverage():
    ob = Obligation("ob", "payroll", Decimal("50000"), date(2026, 9, 10))
    ctx = make_context(cash="1000", positions=(SG, BI), obligations=(ob,),
                       operating_reserve=Decimal("0"), investment_policy=_pol("1"))
    prop = make_proposal("s7", [make_order("s7", 0, "SGOV", Side.SELL, "100", "100.69"),
                                make_order("s7", 1, "BIL", Side.SELL, "100", "92.00")])
    a = assess_partial_fill_safety(prop, ctx, ENGINE)
    assert a.zero_fill_covers_obligations is False
    assert any("zero-fill" in v for v in a.violations)
    assert not a.safe


def test_sell_subsets_are_NOT_checked_for_concentration():
    """ATTACK: a sell-only subset can breach CHECK-04 and is never evaluated
    for it. Start SGOV 60% / BIL 40%. The FULL proposal (sell both) lands at
    66.7% and passes. The BIL-only subset leaves SGOV at 80% and is never
    concentration-tested. SPEC s12 scopes concentration-subset checking to
    BUY proposals, so this is an interpretation gap, not a spec deviation."""
    prices = {"SGOV": Decimal("100.00"), "BIL": Decimal("100.00")}
    sg = Position("SGOV", Decimal("600"), Decimal("600"), Decimal("60000"))
    bi = Position("BIL", Decimal("400"), Decimal("400"), Decimal("40000"))
    ctx = make_context(cash="100000", positions=(sg, bi), obligations=(),
                       operating_reserve=Decimal("0"), prices=prices,
                       investment_policy=_pol("0.70"))
    prop = make_proposal("s8", [make_order("s8", 0, "SGOV", Side.SELL, "300", "100.00"),
                                make_order("s8", 1, "BIL", Side.SELL, "250", "100.00")])
    # full proposal: SGOV 30000 / BIL 15000 -> 66.67% -> passes
    assert evaluate(prop, ctx).result_for(CheckId.CHECK_04).passed

    # the BIL-only subset really would violate: SGOV 60000 / BIL 15000 -> 80%
    only_bil = make_proposal("s8b", [make_order("s8b", 0, "BIL", Side.SELL, "250", "100.00")])
    assert not evaluate(only_bil, ctx).result_for(CheckId.CHECK_04).passed

    # ...but partial-fill assessment never evaluates concentration on sell subsets
    a = assess_partial_fill_safety(prop, ctx, ENGINE)
    assert not any("CHECK-04" in v for v in a.violations)
    assert a.safe, "assessment reports SAFE despite the breaching subset"


def test_leg_count_ceiling_fails_closed():
    ctx = make_context(positions=(), investment_policy=_pol("1"))
    legs = [make_order("s9", i, "SGOV", Side.BUY, "1", "100.69")
            for i in range(MAX_ENUMERATED_LEGS + 1)]
    a = assess_partial_fill_safety(make_proposal("s9", legs), ctx, ENGINE)
    assert not a.safe
    assert any("too many buy legs" in v for v in a.violations)
    assert a.subsets_evaluated == 0


def test_partial_fill_assessment_is_not_invoked_by_the_policy_engine():
    """ATTACK: assess_partial_fill_safety is an advisory API. A proposal that
    is partial-fill UNSAFE still passes TreasuryGuardEngine.evaluate() and is
    AUTO-authorised."""
    from helpers import decide
    ctx = make_context(positions=(), investment_policy=_pol("0.70"))
    prop = make_proposal("s10", [make_order("s10", 0, "SGOV", Side.BUY, "50", "100.69"),
                                 make_order("s10", 1, "BIL", Side.BUY, "55", "92.00")])
    assert not assess_partial_fill_safety(prop, ctx, ENGINE).safe
    assert evaluate(prop, ctx).passed
    assert decide(prop, ctx).result.value == "AUTO"
