"""P2: challenge the builder's spec interpretations."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from helpers import decide, evaluate, make_context, make_order, make_proposal
from opaca.domain.models import (
    CheckId, InvestmentPolicy, Obligation, Position, PrecloseBlackoutConfig, Side,
)


def _pol(conc="0.70", blackout=True):
    return InvestmentPolicy(
        permitted_symbols=frozenset({"SGOV", "BIL", "SHV"}),
        concentration_max_fraction=Decimal(conc),
        min_trade_notional=Decimal("1.00"),
        preclose_blackout=PrecloseBlackoutConfig(enabled=blackout, minutes_before_close=15),
    )


# ---- CHECK-01: gross buy deployment (not netted against proposed sells)
def test_check01_gross_interpretation_blocks_self_funding_rebalance():
    """A rotation SGOV->BIL that is cash-neutral in aggregate is rejected
    because CHECK-01/11 measure GROSS buy notional against settled cash."""
    prices = {"SGOV": Decimal("100.00"), "BIL": Decimal("100.00")}
    sg = Position("SGOV", Decimal("1000"), Decimal("1000"), Decimal("100000"))
    ctx = make_context(cash="1000", positions=(sg,), obligations=(),
                       operating_reserve=Decimal("0"), prices=prices,
                       investment_policy=_pol("1"))
    prop = make_proposal("r1", [make_order("r1", 0, "SGOV", Side.SELL, "500", "100.00"),
                                make_order("r1", 1, "BIL", Side.BUY, "500", "100.00")])
    d = evaluate(prop, ctx)
    assert not d.result_for(CheckId.CHECK_01).passed
    assert not d.result_for(CheckId.CHECK_11).passed
    assert not d.passed, "cash-neutral rotation cannot execute as one proposal"


# ---- CHECK-02: worst-case projected liquidity is absolute, not marginal
def test_check02_is_absolute_so_a_preexisting_shortfall_blocks_everything():
    """If the account is already short of an obligation, EVERY proposal is
    rejected -- including a liquidation that would improve liquidity, when
    the obligation falls before T+1 settlement."""
    ob = Obligation("ob", "payroll", Decimal("90000"), due_date=date(2026, 9, 1))
    sg = Position("SGOV", Decimal("1000"), Decimal("1000"), Decimal("100690"))
    ctx = make_context(cash="10000", positions=(sg,), obligations=(ob,),
                       operating_reserve=Decimal("0"), investment_policy=_pol("1"))
    remedial = make_proposal("r2", [make_order("r2", 0, "SGOV", Side.SELL, "900", "100.69")])
    assert not evaluate(remedial, ctx).result_for(CheckId.CHECK_02).passed


def test_check02_does_permit_a_liquidation_that_settles_before_the_obligation():
    """Control: the same liquidation IS permitted when the obligation falls
    after T+1, so CHECK-02 is not unconditionally pathological."""
    ob = Obligation("ob", "payroll", Decimal("90000"), due_date=date(2026, 9, 15))
    sg = Position("SGOV", Decimal("1000"), Decimal("1000"), Decimal("100690"))
    ctx = make_context(cash="10000", positions=(sg,), obligations=(ob,),
                       operating_reserve=Decimal("0"), investment_policy=_pol("1"))
    remedial = make_proposal("r3", [make_order("r3", 0, "SGOV", Side.SELL, "900", "100.69")])
    d = evaluate(remedial, ctx)
    assert d.result_for(CheckId.CHECK_02).passed
    assert d.result_for(CheckId.CHECK_12).passed


# ---- CHECK-12: sell-only applicability is safe because 01/02 cover buys
def test_check12_vacuous_on_buy_only_but_check02_still_protects_obligations():
    ob = Obligation("ob", "payroll", Decimal("95000"), due_date=date(2026, 9, 10))
    ctx = make_context(cash="100000", obligations=(ob,), operating_reserve=Decimal("0"),
                       investment_policy=_pol("1"))
    prop = make_proposal("r4", [make_order("r4", 0, "SGOV", Side.BUY, "100", "100.69")])
    d = evaluate(prop, ctx)
    assert d.result_for(CheckId.CHECK_12).passed          # vacuous
    assert not d.result_for(CheckId.CHECK_02).passed      # but still blocked
    assert not d.passed


# ---- CHECK-15: disabling the optional blackout removes the ONLY calendar gate
def test_disabling_blackout_authorises_orders_on_a_market_holiday():
    """ATTACK: CHECK-15 is the only check that consults the trading calendar
    for 'is the market open'. With blackout disabled, Labor Day passes."""
    labor = datetime(2026, 9, 7, 14, 30, tzinfo=timezone.utc)
    ctx_off = make_context(now=labor, seed_date=date(2026, 9, 7),
                           investment_policy=_pol("1", blackout=False))
    prop = make_proposal("r5", [make_order("r5", 0, "SGOV", Side.BUY, "10", "100.69")])
    d = evaluate(prop, ctx_off)
    assert d.result_for(CheckId.CHECK_15).passed
    assert d.passed
    assert decide(prop, ctx_off).result.value == "AUTO", \
        "AUTO-authorised on a day the exchange is closed"
    # control: with blackout enabled the same instant fails closed
    ctx_on = make_context(now=labor, seed_date=date(2026, 9, 7),
                          investment_policy=_pol("1", blackout=True))
    assert not evaluate(prop, ctx_on).result_for(CheckId.CHECK_15).passed


def test_blackout_disabled_also_authorises_on_a_saturday():
    sat = datetime(2026, 9, 5, 14, 30, tzinfo=timezone.utc)
    ctx = make_context(now=sat, seed_date=date(2026, 9, 5),
                       investment_policy=_pol("1", blackout=False))
    prop = make_proposal("r6", [make_order("r6", 0, "SGOV", Side.BUY, "10", "100.69")])
    assert evaluate(prop, ctx).passed


# ---- CHECK-13 hard reject: confirmed correct
def test_check13_hard_reject_is_not_overridable(  ):
    from opaca.authority.engine import apply_human_approval
    from opaca.domain.models import AutonomousExecution
    from datetime import timedelta
    from helpers import DEFAULT_NOW
    hist = tuple(AutonomousExecution(DEFAULT_NOW - timedelta(minutes=5 + i), Decimal("1"))
                 for i in range(6))
    ctx = make_context(autonomous_history=hist, investment_policy=_pol("1"))
    prop = make_proposal("r7", [make_order("r7", 0, "SGOV", Side.BUY, "1", "100.69")])
    auth = decide(prop, ctx)
    assert auth.result.value == "REJECT"
    assert apply_human_approval(auth).result.value == "REJECT"


# ---- evaluate(only=...) partial evaluation surface
def test_only_parameter_can_report_passed_without_evaluating_hard_checks():
    """Observation: PolicyDecision.passed is computed over EVALUATED checks
    only, so a caller using only= gets passed=True on an unsafe proposal."""
    ctx = make_context(kill_switch=False, investment_policy=_pol("0.70"))
    from opaca.policy.engine import TreasuryGuardEngine
    prop = make_proposal("r8", [make_order("r8", 0, "SGOV", Side.BUY, "100", "100.69")])
    full = evaluate(prop, ctx)
    assert not full.passed                       # CHECK-04 fails
    narrow = TreasuryGuardEngine().evaluate(prop, ctx, only=frozenset({CheckId.CHECK_03}))
    assert narrow.passed                         # unsafe proposal reported as passing
