"""P0-D: authority splitting."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from helpers import DEFAULT_NOW, decide, evaluate, make_context, make_order, make_proposal
from opaca.domain.models import (
    AuthorityPolicy, AuthorityResult, AutonomousExecution, CheckId,
    InvestmentPolicy, PrecloseBlackoutConfig, Side,
)

# Concentration limit relaxed to 1 so P0-D isolates AUTHORITY only
# (see finding F-01: any single-symbol proposal otherwise fails CHECK-04).
NO_CONC = InvestmentPolicy(
    permitted_symbols=frozenset({"SGOV", "BIL", "SHV"}),
    concentration_max_fraction=Decimal("1"),
    min_trade_notional=Decimal("1.00"),
    preclose_blackout=PrecloseBlackoutConfig(enabled=True, minutes_before_close=15),
)

# per_order 25000 / per_proposal 25000 / rolling24h 50000 / count 10 / runaway 6
POL = AuthorityPolicy(
    per_order_notional_max=Decimal("25000"),
    per_proposal_notional_max=Decimal("25000"),
    rolling_24h_notional_max=Decimal("50000"),
    rolling_order_count_max=10,
    runaway_hourly_order_count_max=6,
)

BIG_CASH = "10000000"


def _ctx(**kw):
    kw.setdefault("cash", BIG_CASH)
    kw.setdefault("obligations", ())
    kw.setdefault("operating_reserve", Decimal("0"))
    kw.setdefault("authority_policy", POL)
    kw.setdefault("investment_policy", NO_CONC)
    return make_context(**kw)


def _buys(pid, notionals, price="100.00"):
    """Build legs whose notional is exactly each value (price 100.00)."""
    legs = []
    for i, n in enumerate(notionals):
        qty = (Decimal(n) / Decimal(price))
        legs.append(make_order(pid, i, ["SGOV", "BIL", "SHV"][i % 3], Side.BUY, qty, price))
    return make_proposal(pid, legs)


def test_one_large_order_requires_approval_not_auto():
    d = decide(_buys("d1", ["25000.01"]), _ctx())
    assert d.result is AuthorityResult.APPROVAL_REQUIRED


def test_exact_per_order_threshold_is_auto():
    d = decide(_buys("d2", ["25000.00"]), _ctx())
    assert d.result is AuthorityResult.AUTO


def test_threshold_plus_one_cent_requires_approval():
    d = decide(_buys("d3", ["25000.01"]), _ctx())
    assert d.result is AuthorityResult.APPROVAL_REQUIRED
    assert any("per-order" in r for r in d.reasons)


def test_many_small_legs_in_one_proposal_cannot_bypass_per_proposal_cap():
    """Split 30,000 into 6 legs of 5,000: each leg is under the per-order cap."""
    d = decide(_buys("d4", ["5000"] * 6), _ctx())
    assert d.result is AuthorityResult.APPROVAL_REQUIRED
    assert any("per-proposal aggregate" in r for r in d.reasons)


def test_multiple_proposals_cannot_bypass_rolling_24h_notional():
    """Two prior autonomous executions of 24,000 each; a third 24,000 proposal
    would take the rolling total to 72,000 > 50,000."""
    hist = (
        AutonomousExecution(timestamp=DEFAULT_NOW - timedelta(hours=3), notional=Decimal("24000")),
        AutonomousExecution(timestamp=DEFAULT_NOW - timedelta(hours=2), notional=Decimal("24000")),
    )
    d = decide(_buys("d5", ["24000"]), _ctx(autonomous_history=hist))
    assert d.result is AuthorityResult.APPROVAL_REQUIRED
    assert any("rolling 24h" in r for r in d.reasons)


def test_rolling_24h_boundary_event_exactly_24h_old_is_excluded():
    hist = (AutonomousExecution(timestamp=DEFAULT_NOW - timedelta(hours=24),
                                notional=Decimal("40000")),)
    d = decide(_buys("d6", ["20000"]), _ctx(autonomous_history=hist))
    assert d.result is AuthorityResult.AUTO, "exactly-24h-old event must fall outside window"


def test_rolling_24h_boundary_event_23h59m59s_old_is_included():
    hist = (AutonomousExecution(timestamp=DEFAULT_NOW - timedelta(hours=23, minutes=59, seconds=59),
                                notional=Decimal("40000")),)
    d = decide(_buys("d7", ["20000"]), _ctx(autonomous_history=hist))
    assert d.result is AuthorityResult.APPROVAL_REQUIRED


def test_rolling_order_count_boundary():
    base = tuple(
        AutonomousExecution(timestamp=DEFAULT_NOW - timedelta(hours=5, minutes=i),
                            notional=Decimal("1"))
        for i in range(9)
    )
    # 9 prior + 1 leg = 10 == limit -> AUTO
    assert decide(_buys("d8", ["100"]), _ctx(autonomous_history=base)).result is AuthorityResult.AUTO
    # 9 prior + 2 legs = 11 > limit -> APPROVAL_REQUIRED
    d = decide(_buys("d9", ["100", "100"]), _ctx(autonomous_history=base))
    assert d.result is AuthorityResult.APPROVAL_REQUIRED
    assert any("rolling autonomous order count" in r for r in d.reasons)


def test_check13_is_hard_reject_and_human_approval_cannot_override():
    """6 orders in the rolling hour already; 1 more leg = 7 > 6."""
    from opaca.authority.engine import apply_human_approval
    hist = tuple(
        AutonomousExecution(timestamp=DEFAULT_NOW - timedelta(minutes=5 + i),
                            notional=Decimal("1"))
        for i in range(6)
    )
    ctx = _ctx(autonomous_history=hist)
    prop = _buys("d10", ["100"])
    decision = evaluate(prop, ctx)
    assert not decision.result_for(CheckId.CHECK_13).passed
    auth = decide(prop, ctx)
    assert auth.result is AuthorityResult.REJECT
    assert apply_human_approval(auth).result is AuthorityResult.REJECT


def test_check07_soft_vs_check13_hard_interaction():
    """CHECK-07 breach alone must be APPROVAL_REQUIRED (recoverable);
    CHECK-13 breach must be REJECT (unrecoverable)."""
    from opaca.authority.engine import apply_human_approval
    ctx = _ctx()
    prop = _buys("d11", ["25000.01"])
    decision = evaluate(prop, ctx)
    assert not decision.result_for(CheckId.CHECK_07).passed
    assert decision.result_for(CheckId.CHECK_07).hard is False
    assert decision.passed, "soft CHECK-07 must not fail the policy decision"
    auth = decide(prop, ctx)
    assert auth.result is AuthorityResult.APPROVAL_REQUIRED
    assert apply_human_approval(auth).result is AuthorityResult.AUTO


def test_runaway_counts_legs_not_proposals():
    """Splitting one action into many legs must not evade CHECK-13."""
    ctx = _ctx()
    prop = _buys("d12", ["100"] * 7)
    assert not evaluate(prop, ctx).result_for(CheckId.CHECK_13).passed


def test_sell_notional_counts_toward_authority_aggregate():
    from opaca.domain.models import Position
    pos = Position("SGOV", Decimal("1000"), Decimal("1000"), Decimal("100690"))
    ctx = _ctx(positions=(pos,))
    prop = make_proposal("d13", [
        make_order("d13", 0, "SGOV", Side.SELL, "300", "100.00")  # 30,000 sell
    ])
    d = decide(prop, ctx)
    assert d.result is AuthorityResult.APPROVAL_REQUIRED
    assert any("per-proposal aggregate" in r or "per-order" in r for r in d.reasons)
