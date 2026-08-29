"""Long-only invariant (CHECK-16).

Required proofs 5 and 6: negative projected positions and sells beyond the
reconciled long position are rejected. Broker shorting capability
(Phase -1A observed shorting_enabled: true) is never policy permission.
"""

from __future__ import annotations

from decimal import Decimal

from opaca.domain.models import CheckId, Position, Side

from tests.helpers import evaluate, make_context, make_order, make_proposal

PRICE = Decimal("100.00")
PRICES = {"SGOV": PRICE, "BIL": Decimal("50.00"), "SHV": Decimal("110.00")}


def sgov_position(quantity: str, available: str | None = None) -> Position:
    qty = Decimal(quantity)
    return Position(
        symbol="SGOV",
        quantity=qty,
        quantity_available=Decimal(available) if available is not None else qty,
        market_value=qty * PRICE,
    )


class TestProjectedNegativePosition:
    def test_projected_negative_position_is_rejected(self) -> None:
        """Required proof 5."""
        context = make_context(prices=PRICES, positions=(sgov_position("5"),))
        proposal = make_proposal(
            "prop-short",
            [make_order("prop-short", 0, "SGOV", Side.SELL, "10", PRICE)],
        )
        decision = evaluate(proposal, context)
        assert not decision.passed
        result = decision.result_for(CheckId.CHECK_16)
        assert not result.passed
        assert "negative" in result.detail

    def test_sell_of_unheld_symbol_is_rejected(self) -> None:
        context = make_context(prices=PRICES)
        proposal = make_proposal(
            "prop-short-2",
            [make_order("prop-short-2", 0, "BIL", Side.SELL, "1", "50.00")],
        )
        decision = evaluate(proposal, context)
        assert not decision.passed
        assert not decision.result_for(CheckId.CHECK_16).passed


class TestSellCannotExceedReconciledLong:
    def test_sell_larger_than_reconciled_long_is_rejected(self) -> None:
        """Required proof 6."""
        context = make_context(prices=PRICES, positions=(sgov_position("100"),))
        proposal = make_proposal(
            "prop-oversell",
            [make_order("prop-oversell", 0, "SGOV", Side.SELL, "100.000000001", PRICE)],
        )
        decision = evaluate(proposal, context)
        assert not decision.passed
        result = decision.result_for(CheckId.CHECK_16)
        assert not result.passed
        assert "exceeds reconciled long position" in result.detail

    def test_sell_beyond_quantity_available_is_rejected(self) -> None:
        position = sgov_position("10", available="4")
        context = make_context(prices=PRICES, positions=(position,))
        proposal = make_proposal(
            "prop-avail",
            [make_order("prop-avail", 0, "SGOV", Side.SELL, "5", PRICE)],
        )
        decision = evaluate(proposal, context)
        assert not decision.result_for(CheckId.CHECK_16).passed

    def test_split_sells_are_aggregated_per_symbol(self) -> None:
        context = make_context(prices=PRICES, positions=(sgov_position("10"),))
        proposal = make_proposal(
            "prop-split-sell",
            [
                make_order("prop-split-sell", 0, "SGOV", Side.SELL, "6", PRICE),
                make_order("prop-split-sell", 1, "SGOV", Side.SELL, "6", PRICE),
            ],
        )
        decision = evaluate(proposal, context)
        assert not decision.result_for(CheckId.CHECK_16).passed

    def test_exact_liquidation_of_reconciled_long_is_permitted(self) -> None:
        context = make_context(
            prices=PRICES,
            positions=(sgov_position("100"),),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-flatten",
            [make_order("prop-flatten", 0, "SGOV", Side.SELL, "100", PRICE)],
        )
        decision = evaluate(proposal, context)
        assert decision.result_for(CheckId.CHECK_16).passed

    def test_broker_shorting_capability_is_not_an_input_to_permission(self) -> None:
        """The Phase -1A account has shorting_enabled=true; the policy engine
        has no path to read account capabilities - only reconciled positions."""
        context = make_context(prices=PRICES)
        assert context.broker.multiplier == Decimal("4")
        proposal = make_proposal(
            "prop-short-3",
            [make_order("prop-short-3", 0, "SGOV", Side.SELL, "1", PRICE)],
        )
        decision = evaluate(proposal, context)
        assert not decision.result_for(CheckId.CHECK_16).passed
