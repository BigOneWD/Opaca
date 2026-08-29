"""Reservation-aware long-only protection (CHECK-16, red-team RT-01).

An unresolved same-direction SELL is reserved locally so that

    effective_available(symbol) =
        min(
            broker quantity_available,
            reconciled position quantity
              - locally reserved unresolved SELL remaining quantity
        )

The ``min`` guards against double subtraction: Alpaca may already have
decremented ``quantity_available`` for an acknowledged order. When an
unresolved SELL's remaining quantity cannot be determined safely
(e.g. UNKNOWN with unknown size), additional sells of that symbol fail
closed.

Orchestration invariant NOT covered here: two truly simultaneous
evaluations against the same snapshot still require an ATOMIC SQLite
reservation (evaluate -> reserve -> persist under the single-writer
transaction) before broker submission; the stateless engine alone does
not solve simultaneous callers.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from opaca.domain.models import (
    AuthorityResult,
    CheckId,
    OrderState,
    Position,
    Proposal,
    Side,
    UnresolvedOrder,
)
from opaca.policy.client_order_id import deterministic_client_order_id
from opaca.policy.engine import effective_available_quantity, sell_reservations

from tests.helpers import decide, evaluate, make_context, make_order, make_proposal

PRICE = Decimal("100.00")
PRICES = {"SGOV": PRICE}


def position(quantity: str, available: str | None = None) -> Position:
    qty = Decimal(quantity)
    return Position(
        symbol="SGOV",
        quantity=qty,
        quantity_available=Decimal(available) if available is not None else qty,
        market_value=qty * PRICE,
    )


def sell(pid: str, quantity: str) -> Proposal:
    return make_proposal(pid, [make_order(pid, 0, "SGOV", Side.SELL, quantity, PRICE)])


def unresolved_sell(
    state: OrderState,
    quantity: Decimal | None = None,
    filled: Decimal | None = None,
    proposal_id: str = "prior",
) -> UnresolvedOrder:
    return UnresolvedOrder(
        proposal_id=proposal_id,
        symbol="SGOV",
        side=Side.SELL,
        client_order_id="opaca-prior",
        state=state,
        quantity=quantity,
        filled_quantity=filled,
    )


class TestEffectiveAvailable:
    def test_stale_broker_available_is_bounded_by_local_reservation(self) -> None:
        """quantity_available still reads 100 (broker has not reserved the
        unresolved sell); the local reservation of 70 binds."""
        available = effective_available_quantity(position("100"), Decimal("70"), False)
        assert available == Decimal("30")

    def test_no_double_subtraction_when_broker_already_reserved(self) -> None:
        """Broker quantity_available already reflects the acknowledged sell
        (30). The reservation must not be subtracted a second time."""
        available = effective_available_quantity(position("100", "30"), Decimal("70"), False)
        assert available == Decimal("30")

    def test_undeterminable_reservation_fails_closed(self) -> None:
        assert effective_available_quantity(position("100"), Decimal("0"), True) == Decimal("0")

    def test_missing_position_fails_closed(self) -> None:
        assert effective_available_quantity(None, Decimal("0"), False) == Decimal("0")


class TestSellReservations:
    def test_unresolved_sell_states_reserve_remaining_quantity(self) -> None:
        for state in (
            OrderState.PROPOSED,
            OrderState.SUBMITTED,
            OrderState.NEW,
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.UNKNOWN,
        ):
            reserved, undeterminable = sell_reservations(
                (unresolved_sell(state, quantity=Decimal("70")),)
            )
            assert reserved == {"SGOV": Decimal("70")}, state
            assert undeterminable == frozenset(), state

    def test_terminal_orders_reserve_nothing(self) -> None:
        for state in (
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.CANCELED_REMAINDER,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.RECONCILED,
        ):
            reserved, undeterminable = sell_reservations(
                (unresolved_sell(state, quantity=Decimal("70")),)
            )
            assert reserved == {}, state
            assert undeterminable == frozenset(), state

    def test_unresolved_buys_do_not_reserve(self) -> None:
        order = UnresolvedOrder(
            proposal_id="prior",
            symbol="SGOV",
            side=Side.BUY,
            client_order_id="opaca-prior",
            state=OrderState.NEW,
            quantity=Decimal("70"),
        )
        reserved, undeterminable = sell_reservations((order,))
        assert reserved == {}
        assert undeterminable == frozenset()

    def test_unknown_sell_with_unknown_quantity_is_undeterminable(self) -> None:
        reserved, undeterminable = sell_reservations((unresolved_sell(OrderState.UNKNOWN),))
        assert reserved == {}
        assert undeterminable == frozenset({"SGOV"})

    def test_multiple_unresolved_sells_aggregate(self) -> None:
        reserved, undeterminable = sell_reservations(
            (
                unresolved_sell(OrderState.NEW, quantity=Decimal("40"), proposal_id="p1"),
                unresolved_sell(OrderState.ACCEPTED, quantity=Decimal("30"), proposal_id="p2"),
            )
        )
        assert reserved == {"SGOV": Decimal("70")}
        assert undeterminable == frozenset()

    def test_invalid_filled_quantity_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            unresolved_sell(
                OrderState.PARTIALLY_FILLED, quantity=Decimal("10"), filled=Decimal("20")
            )
        with pytest.raises(ValueError):
            unresolved_sell(OrderState.NEW, quantity=Decimal("-1"))


class TestCheck16Reservation:
    def test_stale_available_100_with_reserved_sell_70_leaves_30(self) -> None:
        context = make_context(
            prices=PRICES,
            positions=(position("100"),),
            unresolved_orders=(unresolved_sell(OrderState.NEW, quantity=Decimal("70")),),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        assert evaluate(sell("p-ok", "30"), context).result_for(CheckId.CHECK_16).passed
        assert not evaluate(sell("p-no", "31"), context).result_for(CheckId.CHECK_16).passed

    def test_broker_already_decremented_available_no_double_subtraction(self) -> None:
        context = make_context(
            prices=PRICES,
            positions=(position("100", "30"),),
            unresolved_orders=(unresolved_sell(OrderState.ACCEPTED, quantity=Decimal("70")),),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        assert evaluate(sell("p-ok", "30"), context).result_for(CheckId.CHECK_16).passed
        assert (
            not evaluate(sell("p-no", "30.000000001"), context).result_for(CheckId.CHECK_16).passed
        )

    def test_unknown_sell_unknown_quantity_blocks_all_additional_sells(self) -> None:
        context = make_context(
            prices=PRICES,
            positions=(position("100"),),
            unresolved_orders=(unresolved_sell(OrderState.UNKNOWN),),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        result = evaluate(sell("p-any", "0.000000001"), context).result_for(CheckId.CHECK_16)
        assert not result.passed
        assert "fail closed" in result.detail

    def test_partially_filled_sell_reserves_remaining_amount(self) -> None:
        context = make_context(
            prices=PRICES,
            positions=(position("100"),),
            unresolved_orders=(
                unresolved_sell(
                    OrderState.PARTIALLY_FILLED, quantity=Decimal("70"), filled=Decimal("30")
                ),
            ),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        assert evaluate(sell("p-ok", "60"), context).result_for(CheckId.CHECK_16).passed
        assert not evaluate(sell("p-no", "61"), context).result_for(CheckId.CHECK_16).passed

    def test_multiple_unresolved_sells_aggregate_against_new_sell(self) -> None:
        context = make_context(
            prices=PRICES,
            positions=(position("100"),),
            unresolved_orders=(
                unresolved_sell(OrderState.NEW, quantity=Decimal("40"), proposal_id="p1"),
                unresolved_sell(OrderState.NEW, quantity=Decimal("40"), proposal_id="p2"),
            ),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        assert evaluate(sell("p-ok", "20"), context).result_for(CheckId.CHECK_16).passed
        assert not evaluate(sell("p-no", "21"), context).result_for(CheckId.CHECK_16).passed

    def test_proposed_legs_aggregate_with_reservations(self) -> None:
        """Reserved 70 + proposed 40 = 110 > reconciled 100."""
        context = make_context(
            prices=PRICES,
            positions=(position("100"),),
            unresolved_orders=(unresolved_sell(OrderState.NEW, quantity=Decimal("70")),),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "p-split",
            [
                make_order("p-split", 0, "SGOV", Side.SELL, "20", PRICE),
                make_order("p-split", 1, "SGOV", Side.SELL, "20", PRICE),
            ],
        )
        assert not evaluate(proposal, context).result_for(CheckId.CHECK_16).passed

    def test_exact_equality_passes(self) -> None:
        context = make_context(
            prices=PRICES,
            positions=(position("100"),),
            unresolved_orders=(unresolved_sell(OrderState.NEW, quantity=Decimal("70")),),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        assert evaluate(sell("p-exact", "30"), context).result_for(CheckId.CHECK_16).passed

    def test_oversell_by_one_quantum_fails(self) -> None:
        context = make_context(
            prices=PRICES,
            positions=(position("100"),),
            unresolved_orders=(unresolved_sell(OrderState.NEW, quantity=Decimal("70")),),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        assert (
            not evaluate(sell("p-over", "30.000000001"), context)
            .result_for(CheckId.CHECK_16)
            .passed
        )

    def test_no_unresolved_sells_keeps_broker_available_binding(self) -> None:
        context = make_context(
            prices=PRICES,
            positions=(position("100", "40"),),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        assert evaluate(sell("p-ok", "40"), context).result_for(CheckId.CHECK_16).passed
        assert not evaluate(sell("p-no", "41"), context).result_for(CheckId.CHECK_16).passed


class TestUnknownSameLogicalOrder:
    def test_unknown_same_logical_sell_remains_reserved(self) -> None:
        """NEW-02: idempotent recovery means reconcile the same logical
        order, not resubmit an uncertain trade. An UNKNOWN sell of this
        proposal still reserves remaining quantity."""
        client_order_id = deterministic_client_order_id("P1", 0)
        own = UnresolvedOrder(
            proposal_id="P1",
            symbol="SGOV",
            side=Side.SELL,
            client_order_id=client_order_id,
            state=OrderState.UNKNOWN,
            quantity=Decimal("100"),
            filled_quantity=Decimal("0"),
        )
        context = make_context(
            prices=PRICES,
            positions=(position("100"),),
            unresolved_orders=(own,),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        retry = make_proposal("P1", [make_order("P1", 0, "SGOV", Side.SELL, "100", PRICE)])
        decision = evaluate(retry, context)
        result = decision.result_for(CheckId.CHECK_16)
        assert not result.passed
        assert "reserved unresolved sells 100" in result.detail
        assert not decision.passed
        assert decide(retry, context).result is AuthorityResult.REJECT
