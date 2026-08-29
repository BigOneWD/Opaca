"""P1-B: partial fill safety."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from helpers import ENGINE, evaluate, make_context, make_order, make_proposal
from opaca.domain.models import (
    AuthorityPolicy, CheckId, InvestmentPolicy, Obligation, Position,
    PrecloseBlackoutConfig, Side,
)

BIGAUTH = AuthorityPolicy(Decimal("1e9"), Decimal("1e9"), Decimal("1e9"), 500, 500)
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


def test_single_leg_fill_no_longer_shows_a_fake_100_percent():
    """RT-02/RT-09 FIXED. Under the Amendment G pool base the unfilled
    investment cash stays in the denominator, so a balanced 2-leg buy whose
    single-leg subsets are well inside the limit is now partial-fill SAFE."""
    ctx = make_context(cash="1000000", positions=(), obligations=(),
                       operating_reserve=Decimal("0"), investment_policy=_pol("0.70"),
                       prices={"SGOV": Decimal("100.00"), "BIL": Decimal("100.00")},
                       authority_policy=BIGAUTH)
    prop = make_proposal("s4", [make_order("s4", 0, "SGOV", Side.BUY, "1000", "100.00"),
                                make_order("s4", 1, "BIL", Side.BUY, "1000", "100.00")])
    assert evaluate(prop, ctx).result_for(CheckId.CHECK_04).passed
    a = assess_partial_fill_safety(prop, ctx, ENGINE)
    assert a.safe
    assert a.subsets_evaluated == 3


def test_a_genuinely_overconcentrated_single_leg_is_still_caught():
    """The enumeration must still bite when a leg alone exceeds the limit."""
    ctx = make_context(cash="1000000", positions=(), obligations=(),
                       operating_reserve=Decimal("0"), investment_policy=_pol("0.70"),
                       prices={"SGOV": Decimal("100.00"), "BIL": Decimal("100.00")},
                       authority_policy=BIGAUTH)
    prop = make_proposal("s4b", [make_order("s4b", 0, "SGOV", Side.BUY, "7500", "100.00"),
                                 make_order("s4b", 1, "BIL", Side.BUY, "100", "100.00")])
    a = assess_partial_fill_safety(prop, ctx, ENGINE)
    assert not a.safe
    assert any("CHECK-04" in v for v in a.violations)


def test_buy_proposals_from_an_empty_portfolio_can_now_be_partial_fill_safe():
    """RT-02 FIXED: the pool base makes from-empty buys reachable."""
    ctx = make_context(cash="1000000", positions=(), obligations=(),
                       operating_reserve=Decimal("0"), investment_policy=_pol("0.70"),
                       prices={"SGOV": Decimal("100.00"), "BIL": Decimal("100.00"),
                               "SHV": Decimal("100.00")},
                       authority_policy=BIGAUTH)
    for n in (2, 3, 4):
        legs = [make_order("s5", i, ["SGOV", "BIL", "SHV"][i % 3], Side.BUY, "500", "100.00")
                for i in range(n)]
        assert assess_partial_fill_safety(make_proposal("s5", legs), ctx, ENGINE).safe


def test_mixed_notional_sizes_dominant_leg_alone():
    """A dominant leg above the pool-base limit is caught on its own subset."""
    ctx = make_context(cash="1000000", positions=(), obligations=(),
                       operating_reserve=Decimal("0"), investment_policy=_pol("0.70"),
                       prices={"SGOV": Decimal("100.00"), "BIL": Decimal("100.00")},
                       authority_policy=BIGAUTH)
    prop = make_proposal("s6", [make_order("s6", 0, "SGOV", Side.BUY, "9000", "100.00"),
                                make_order("s6", 1, "BIL", Side.BUY, "10", "100.00")])
    assert not assess_partial_fill_safety(prop, ctx, ENGINE).safe


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


def test_sell_subsets_are_now_concentration_checked():
    """RT-09 FIXED. Enumeration covers every subset of ALL legs, and CHECK-04
    and CHECK-16 are both in the per-subset control set."""
    from opaca.policy.partial_fill import PARTIAL_FILL_CHECKS
    assert CheckId.CHECK_04 in PARTIAL_FILL_CHECKS
    assert CheckId.CHECK_16 in PARTIAL_FILL_CHECKS
    prices = {"SGOV": Decimal("100.00"), "BIL": Decimal("100.00")}
    sg = Position("SGOV", Decimal("600"), Decimal("600"), Decimal("60000"))
    bi = Position("BIL", Decimal("400"), Decimal("400"), Decimal("40000"))
    ctx = make_context(cash="1", positions=(sg, bi), obligations=(),
                       operating_reserve=Decimal("0"), prices=prices,
                       investment_policy=_pol("0.70"), authority_policy=BIGAUTH)
    prop = make_proposal("s8", [make_order("s8", 0, "SGOV", Side.SELL, "300", "100.00"),
                                make_order("s8", 1, "BIL", Side.SELL, "250", "100.00")])
    a = assess_partial_fill_safety(prop, ctx, ENGINE)
    assert a.subsets_evaluated == 3, "both sides must be enumerated"


def test_leg_count_ceiling_fails_closed():
    ctx = make_context(positions=(), investment_policy=_pol("1"))
    legs = [make_order("s9", i, "SGOV", Side.BUY, "1", "100.69")
            for i in range(MAX_ENUMERATED_LEGS + 1)]
    a = assess_partial_fill_safety(make_proposal("s9", legs), ctx, ENGINE)
    assert not a.safe
    assert any("too many buy legs" in v for v in a.violations)
    assert a.subsets_evaluated == 0


def test_partial_fill_assessment_is_now_in_the_authority_path():
    """RT-06 FIXED. AUTO is unreachable without a SAFE assessment, and an
    unassessed proposal fails closed."""
    from opaca.authority.engine import decide_authority
    from opaca.domain.models import AuthorityResult
    from helpers import decide
    ctx = make_context(cash="1000000", positions=(), obligations=(),
                       operating_reserve=Decimal("0"), investment_policy=_pol("0.70"),
                       prices={"SGOV": Decimal("100.00")}, authority_policy=BIGAUTH)
    prop = make_proposal("s10", [make_order("s10", 0, "SGOV", Side.BUY, "1000", "100.00")])
    decision = evaluate(prop, ctx)
    assert decision.passed
    bare = decide_authority(prop, decision, ctx.authority_policy,
                            ctx.autonomous_history, ctx.execution.now)
    assert bare.result is AuthorityResult.REJECT
    assert decide(prop, ctx).result is AuthorityResult.AUTO


