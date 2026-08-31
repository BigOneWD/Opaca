"""S5: bounded BUY limit, cent boundaries, and maximum cash exposure."""
from __future__ import annotations

from decimal import ROUND_UP, Decimal

import pytest
from opaca.domain.models import Side
from opaca.execution.service import execute_reserved_proposal
from opaca.market.errors import QuoteValidationError
from opaca.market.limit import (
    DEFAULT_BUY_LIMIT_TOLERANCE,
    buy_limit_price,
    max_buy_cash_obligation,
    sell_modeled_price,
)
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.reconciliation.service import reconcile

from support import DEFAULT_NOW, bind_buy, bind_sell, bind_single_leg_proposal, quotes_for, world


def test_default_tolerance_is_ten_basis_points():
    assert DEFAULT_BUY_LIMIT_TOLERANCE == Decimal("0.001")


@pytest.mark.parametrize("canonical,expected", [
    ("99.99", "100.09"),      # 100.08999  -> up
    ("100.00", "100.10"),     # 100.1      -> exact
    ("100.01", "100.12"),     # 100.11001  -> up
    ("100.69", "100.80"),     # 100.79069  -> up
    ("0.01", "0.02"),         # 0.01001    -> up
    ("1.00", "1.01"),         # 1.001      -> up
    ("100.005", "100.11"),    # 100.105005 -> up
    ("100.695", "100.80"),    # 100.795695 -> up
    ("100.999", "101.10"),    # 101.099999 -> up
    ("92.00", "92.10"),       # 92.092     -> up
    ("110.00", "110.11"),     # 110.11     -> exact
])
def test_buy_limit_is_canonical_times_one_plus_tolerance_rounded_up(canonical, expected):
    got = buy_limit_price(Decimal(canonical))
    assert got == Decimal(expected)
    assert got == (Decimal(canonical) * Decimal("1.001")).quantize(
        Decimal("0.01"), rounding=ROUND_UP)
    assert got.as_tuple().exponent == -2
    assert got >= Decimal(canonical)


def test_the_limit_is_never_below_the_canonical_print():
    cents = [Decimal(f"{whole}.{frac:02d}") for whole in (0, 1, 99, 100, 4999)
             for frac in (0, 1, 49, 50, 99)]
    for price in cents:
        if price <= 0:
            continue
        assert buy_limit_price(price) >= price


def test_a_zero_tolerance_still_rounds_up_to_a_valid_cent():
    assert buy_limit_price(Decimal("100.695"), tolerance=Decimal("0")) == Decimal("100.70")
    assert buy_limit_price(Decimal("100.69"), tolerance=Decimal("0")) == Decimal("100.69")


def test_a_negative_tolerance_fails_closed():
    for bad in ("-0.001", "-1"):
        with pytest.raises(QuoteValidationError):
            buy_limit_price(Decimal("100.69"), tolerance=Decimal(bad))


@pytest.mark.parametrize("bad", ["0", "-1", "NaN", "Infinity"])
def test_a_non_positive_canonical_price_fails_closed(bad):
    with pytest.raises(Exception):
        buy_limit_price(Decimal(bad))


def test_max_cash_obligation_is_quantity_times_limit():
    q = quotes_for()["SGOV"]
    bound = bind_buy(q, Decimal("7"))
    assert bound.limit_price == Decimal("100.80")
    assert bound.max_cash_obligation == Decimal("7") * Decimal("100.80")
    assert bound.max_cash_obligation == max_buy_cash_obligation(Decimal("7"), Decimal("100.80"))
    assert bound.max_cash_obligation > Decimal("7") * q.price


def test_the_proposed_leg_notional_equals_quantity_times_limit():
    quotes = quotes_for()
    bound = bind_buy(quotes["SGOV"], Decimal("4"))
    proposal, prices, bindings = bind_single_leg_proposal("n1", bound, quotes)
    assert proposal.legs[0].reference_price == bound.limit_price
    assert proposal.total_buy_notional == bound.max_cash_obligation
    assert prices["SGOV"] == quotes["SGOV"].price


def test_a_sell_is_never_marked_above_the_canonical_print():
    q = quotes_for()["SGOV"]
    bound = bind_sell(q, Decimal("10"))
    assert bound.reference_price == q.price == sell_modeled_price(q)
    assert bound.limit_price == q.price
    assert bound.max_cash_obligation is None


def test_reserved_cash_uses_the_limit_never_buying_power_or_the_last_trade(tmp_path):
    w = world(tmp_path, qty="0", cash="100000")
    assert Decimal(str(w.account["buying_power"])) > Decimal(str(w.account["cash"]))
    quotes = quotes_for()
    bound = bind_buy(quotes["SGOV"], Decimal("5"))
    proposal, prices, bindings = bind_single_leg_proposal("cash5", bound, quotes)
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    out = evaluate_and_reserve(
        w.store, proposal, now=DEFAULT_NOW, prices=prices,
        expected_snapshot_version=recon.snapshot.version, price_bindings=bindings)
    assert out.is_auto is True
    from opaca.persistence.types import ReservationKind, ReservationStatus
    cash = sum((r.amount for r in w.store.active_reservations()
                if r.kind is ReservationKind.CASH_DEPLOYMENT
                and r.status is ReservationStatus.ACTIVE and r.amount is not None),
               Decimal("0"))
    assert cash == Decimal("5") * bound.limit_price
    assert cash != Decimal("5") * quotes["SGOV"].price
    assert cash < Decimal(str(w.account["buying_power"]))
    w.close()


def test_the_submitted_buy_is_a_day_limit_at_the_bound_price(tmp_path):
    w = world(tmp_path, qty="0", cash="100000")
    quotes = quotes_for()
    bound = bind_buy(quotes["SGOV"], Decimal("1"))
    proposal, prices, bindings = bind_single_leg_proposal("day1", bound, quotes)
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    assert evaluate_and_reserve(
        w.store, proposal, now=DEFAULT_NOW, prices=prices,
        expected_snapshot_version=recon.snapshot.version,
        price_bindings=bindings).is_auto is True

    seen = []

    class Recording(type(w.mutate())):
        def submit_order(self, request):
            seen.append(request)
            return super().submit_order(request)

    mutate = Recording(orders=w.orders, linked_account=w.account,
                       linked_positions=w.positions, linked_open_orders=w.open_orders,
                       fill_price=quotes["SGOV"].price)
    execute_reserved_proposal(
        w.store, w.read(), mutate, proposal, now=DEFAULT_NOW, prices=prices,
        price_bindings=bindings)
    w.close()
    assert len(seen) == 1
    req = seen[0]
    assert req.order_type == "limit"
    assert req.time_in_force == "day"
    assert req.side is Side.BUY
    assert req.limit_price == bound.limit_price == Decimal("100.80")
    assert req.limit_price > quotes["SGOV"].price


def test_a_market_order_request_is_still_rejected_if_it_carries_a_limit():
    from opaca.execution.gateway import PaperOrderRequest
    with pytest.raises(ValueError):
        PaperOrderRequest(symbol="SGOV", side=Side.BUY, quantity=Decimal("1"),
                          client_order_id="opaca-" + "a" * 32,
                          order_type="market", limit_price=Decimal("100.80"))
    with pytest.raises(ValueError):
        PaperOrderRequest(symbol="SGOV", side=Side.BUY, quantity=Decimal("1"),
                          client_order_id="opaca-" + "a" * 32, order_type="limit")
    with pytest.raises(ValueError):
        PaperOrderRequest(symbol="SGOV", side=Side.BUY, quantity=Decimal("1"),
                          client_order_id="opaca-" + "a" * 32, order_type="stop",
                          limit_price=Decimal("100.80"))
    with pytest.raises(ValueError):
        PaperOrderRequest(symbol="SGOV", side=Side.BUY, quantity=Decimal("1"),
                          client_order_id="opaca-" + "a" * 32, order_type="limit",
                          limit_price=Decimal("0"), time_in_force="day")
    with pytest.raises(ValueError):
        PaperOrderRequest(symbol="SGOV", side=Side.BUY, quantity=Decimal("1"),
                          client_order_id="opaca-" + "a" * 32, order_type="limit",
                          limit_price=Decimal("100.80"), time_in_force="gtc")


def test_ROUNDING_max_cash_obligation_rounds_the_ceiling_down(tmp_path):
    """round_budget is ROUND_DOWN; for a fractional quantity the stated maximum
    is up to one cent below qty x LIMIT."""
    exact = Decimal("0.333") * Decimal("100.80")
    stated = max_buy_cash_obligation(Decimal("0.333"), Decimal("100.80"))
    assert stated <= exact
    assert exact - stated < Decimal("0.01")
    assert stated == Decimal("33.56")
