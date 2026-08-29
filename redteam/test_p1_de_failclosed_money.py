"""P1-D fail-closed on corrupt inputs; P1-E money/Decimal boundaries."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from helpers import (
    DEFAULT_NOW, decide, evaluate, make_context, make_order, make_proposal,
)
from opaca.domain.models import (
    AssetState, AssetStatus, AuthorityPolicy, BrokerCashState, CheckId,
    InvestmentPolicy, Obligation, Position, PrecloseBlackoutConfig,
    ProposedOrder, SettlementEvent, Side,
)
from opaca.domain.money import MoneyError, money, round_quantity
from opaca.treasury.liquidity import MissingPriceError, project_portfolio

BUY = lambda pid="f": make_proposal(pid, [make_order(pid, 0, "SGOV", Side.BUY, "1", "100.69")])


# ------------------------------------------------------------------ P1-D
def test_missing_price_fails_closed_on_check04_and_check11():
    ctx = make_context(prices={"BIL": Decimal("92.00")})
    d = evaluate(BUY("f1"), ctx)
    assert not d.result_for(CheckId.CHECK_04).passed
    assert not d.result_for(CheckId.CHECK_11).passed
    assert "fail closed" in d.result_for(CheckId.CHECK_04).detail
    assert not d.passed


def test_missing_asset_state_fails_closed_on_check05():
    ctx = make_context(assets={})
    d = evaluate(BUY("f2"), ctx)
    assert not d.result_for(CheckId.CHECK_05).passed
    assert "fail closed" in d.result_for(CheckId.CHECK_05).detail


def test_inactive_or_untradable_asset_rejected():
    for status, tradable in ((AssetStatus.INACTIVE, True), (AssetStatus.ACTIVE, False)):
        ctx = make_context(assets={"SGOV": AssetState("SGOV", status, tradable, True)})
        assert not evaluate(BUY("f3"), ctx).result_for(CheckId.CHECK_05).passed


def test_missing_position_makes_any_sell_fail_closed():
    ctx = make_context(positions=())
    prop = make_proposal("f4", [make_order("f4", 0, "SGOV", Side.SELL, "1", "100.69")])
    assert not evaluate(prop, ctx).result_for(CheckId.CHECK_16).passed


def test_kill_switch_short_circuits_and_leaves_other_checks_unevaluated():
    d = evaluate(BUY("f5"), make_context(kill_switch=True))
    assert not d.passed
    assert d.result_for(CheckId.CHECK_00).passed is False
    with pytest.raises(KeyError):
        d.result_for(CheckId.CHECK_01)
    assert decide(BUY("f5"), make_context(kill_switch=True)).result.value == "REJECT"


def test_unverified_or_live_environment_fails_closed():
    assert not evaluate(BUY("f6"), make_context(environment_verified=False)).result_for(
        CheckId.CHECK_08).passed
    from opaca.domain.models import BrokerEnvironment
    assert not evaluate(BUY("f7"), make_context(
        environment=BrokerEnvironment.LIVE)).result_for(CheckId.CHECK_08).passed


def test_empty_permitted_symbols_rejected_at_construction():
    with pytest.raises(ValueError):
        InvestmentPolicy(frozenset(), Decimal("0.7"), Decimal("1"),
                         PrecloseBlackoutConfig(True, 15))


def test_corrupt_client_order_id_fails_check09():
    leg = ProposedOrder("f8", 0, "SGOV", Side.BUY, Decimal("1"), Decimal("100.69"), "tampered")
    d = evaluate(make_proposal("f8", [leg]), make_context())
    assert not d.result_for(CheckId.CHECK_09).passed


def test_duplicate_leg_index_fails_check09():
    legs = [make_order("f9", 0, "SGOV", Side.BUY, "1", "100.69"),
            make_order("f9", 0, "BIL", Side.BUY, "1", "92.00")]
    d = evaluate(make_proposal("f9", legs), make_context())
    assert not d.result_for(CheckId.CHECK_09).passed


def test_negative_or_invalid_constructor_values_raise():
    with pytest.raises(MoneyError):
        Position("SGOV", Decimal("-1"), Decimal("-1"), Decimal("0"))
    with pytest.raises(ValueError):
        Position("SGOV", Decimal("1"), Decimal("2"), Decimal("0"))   # avail > qty
    with pytest.raises(MoneyError):
        Obligation("o", "n", Decimal("0"), date(2026, 9, 10))        # must be > 0
    with pytest.raises(ValueError):
        SettlementEvent("e", "SGOV", date(2026, 9, 2), date(2026, 9, 1), Decimal("1"))
    with pytest.raises(ValueError):
        AuthorityPolicy(Decimal("1"), Decimal("1"), Decimal("1"), 0, 1)


def test_empty_proposal_is_vacuously_authorized():
    """Documented edge: a zero-leg proposal passes every check."""
    d = evaluate(make_proposal("f10", []), make_context())
    assert d.passed
    assert decide(make_proposal("f10", []), make_context()).result.value == "AUTO"


# ------------------------------------------------------------------ P1-E
def test_float_construction_forbidden_everywhere():
    with pytest.raises(MoneyError):
        money(1.5)
    with pytest.raises(MoneyError):
        Position("SGOV", 1.5, 1.5, 0.0)
    with pytest.raises(MoneyError):
        Obligation("o", "n", 100.5, date(2026, 9, 10))
    with pytest.raises(MoneyError):
        ProposedOrder("x", 0, "SGOV", Side.BUY, Decimal("1"), 100.69, "opaca-x")


def test_bool_is_not_money():
    with pytest.raises(MoneyError):
        money(True)


def test_nan_and_infinity_rejected():
    for bad in (Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(MoneyError):
            money(bad)
    with pytest.raises(MoneyError):
        Position("SGOV", Decimal("NaN"), Decimal("0"), Decimal("0"))
    with pytest.raises(MoneyError):
        BrokerCashState(Decimal("Infinity"), Decimal("0"), Decimal("0"),
                        Decimal("1"), DEFAULT_NOW)


def test_negative_zero_is_accepted_as_zero():
    assert money(Decimal("-0")) == Decimal("0")
    p = Position("SGOV", Decimal("-0"), Decimal("-0"), Decimal("-0"))
    assert p.quantity == Decimal("0")


def test_very_large_values_now_raise_MoneyError_at_the_boundary():
    """RT-05 FIXED. MAGNITUDE_LIMIT rejects out-of-range magnitudes in
    money(), and every quantize() is wrapped so a raw InvalidOperation can
    never escape a public rounding function."""
    from decimal import InvalidOperation
    from opaca.domain.money import MAGNITUDE_LIMIT, round_budget

    money(MAGNITUDE_LIMIT - Decimal("1"))
    for bad in (MAGNITUDE_LIMIT, Decimal("9") * MAGNITUDE_LIMIT, -MAGNITUDE_LIMIT):
        with pytest.raises(MoneyError):
            money(bad)
    with pytest.raises(MoneyError):
        round_budget(Decimal("9") * MAGNITUDE_LIMIT)
    with pytest.raises(MoneyError):
        BrokerCashState(Decimal("1e30"), Decimal("0"), Decimal("0"),
                        Decimal("1"), DEFAULT_NOW)
    with pytest.raises(MoneyError):
        ProposedOrder("big", 0, "SGOV", Side.BUY, Decimal("1e19"),
                      Decimal("100.69"), "opaca-x")
    try:
        money(Decimal("1e30"))
    except Exception as exc:
        assert isinstance(exc, ValueError)
        assert not isinstance(exc, InvalidOperation)


def test_seed_scenario_now_raises_MoneyError_on_huge_opening_cash():
    from opaca.treasury.scenario import seed_scenario
    with pytest.raises(MoneyError):
        seed_scenario(Decimal("1e30"), date(2026, 9, 1))


def test_quantity_precision_rounds_down_and_rejects_dust_to_zero():
    assert round_quantity(Decimal("1.9999999999")) == Decimal("1.999999999")
    with pytest.raises(MoneyError):
        round_quantity(Decimal("0.0000000001"))
    with pytest.raises(MoneyError):
        ProposedOrder("x", 0, "SGOV", Side.BUY, Decimal("0.0000000001"),
                      Decimal("100.69"), "opaca-x")


def test_notional_rounds_down_never_increasing_budget():
    o = make_order("m", 0, "SGOV", Side.BUY, "3", "100.699")
    assert o.notional == Decimal("302.09")          # 302.097 -> DOWN
    assert o.notional < Decimal("3") * Decimal("100.699")


def test_fractional_cent_notional_below_min_trade_size_is_dust():
    ctx = make_context()
    prop = make_proposal("m2", [make_order("m2", 0, "SGOV", Side.BUY,
                                           "0.000000009", "100.69")])
    d = evaluate(prop, ctx)
    assert not d.result_for(CheckId.CHECK_14).passed   # notional 0.00 < 1.00


def test_min_trade_size_boundary_one_quantum_each_side():
    ctx = make_context()
    below = make_proposal("m3", [make_order("m3", 0, "SGOV", Side.BUY, "0.0099", "100.00")])
    at = make_proposal("m4", [make_order("m4", 0, "SGOV", Side.BUY, "0.01", "100.00")])
    assert not evaluate(below, ctx).result_for(CheckId.CHECK_14).passed   # 0.99
    assert evaluate(at, ctx).result_for(CheckId.CHECK_14).passed          # 1.00


def test_no_implicit_float_anywhere_in_the_projection():
    pos = Position("SGOV", Decimal("1"), Decimal("1"), Decimal("100.69"))
    proj = project_portfolio((pos,), (), {"SGOV": Decimal("100.69")})
    for p in proj.positions:
        assert isinstance(p.projected_market_value, Decimal)
        assert isinstance(p.reference_price, Decimal)
    assert isinstance(proj.total_invested_value, Decimal)
    for v in proj.concentration_by_symbol.values():
        assert isinstance(v, Decimal)
