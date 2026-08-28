"""Concentration (CHECK-04): projected post-trade total invested market
value is the only denominator.

Required proofs 7 and 8.
"""

from __future__ import annotations

from decimal import Decimal

from opaca.domain.models import CheckId, Position, Side
from opaca.policy.engine import CHECK_ORDER
from opaca.treasury.liquidity import project_portfolio

from tests.helpers import evaluate, make_context, make_order, make_proposal

PRICE = Decimal("100.00")
PRICES = {"SGOV": PRICE, "BIL": PRICE, "SHV": PRICE}


def position(symbol: str, quantity: str) -> Position:
    qty = Decimal(quantity)
    return Position(symbol=symbol, quantity=qty, quantity_available=qty, market_value=qty * PRICE)


class TestExistingPositionsIncluded:
    def test_existing_positions_are_included_in_concentration(self) -> None:
        """Required proof 7. The proposal is balanced and small; the
        violation comes from the EXISTING SGOV holding."""
        context = make_context(
            prices=PRICES,
            positions=(position("SGOV", "800"), position("BIL", "200")),
            obligations=(),
            operating_reserve=Decimal("0"),
            # investable ceiling irrelevant: fund via cash 100k
        )
        proposal = make_proposal(
            "prop-conc",
            [
                make_order("prop-conc", 0, "BIL", Side.BUY, "50", PRICE),
                make_order("prop-conc", 1, "SHV", Side.BUY, "50", PRICE),
            ],
        )
        decision = evaluate(proposal, context)
        result = decision.result_for(CheckId.CHECK_04)
        assert not result.passed
        assert "SGOV" in result.detail
        portfolio = project_portfolio(context.positions, proposal.legs, PRICES)
        assert portfolio.concentration_of("SGOV") == Decimal("80000.00") / Decimal("110000.00")

    def test_pre_existing_breach_blocks_even_balanced_buys(self) -> None:
        context = make_context(
            prices=PRICES,
            positions=(position("SGOV", "800"), position("BIL", "200")),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-conc-2",
            [make_order("prop-conc-2", 0, "BIL", Side.BUY, "10", PRICE)],
        )
        decision = evaluate(proposal, context)
        assert not decision.result_for(CheckId.CHECK_04).passed


class TestCorrectDenominator:
    def test_denominator_is_projected_post_trade_invested_value(self) -> None:
        """Required proof 8. A proposal-only view would call the single SHV
        leg 100% of the proposal and fail; the correct denominator (existing
        + proposed) yields 23.1% and passes."""
        context = make_context(
            prices=PRICES,
            positions=(position("BIL", "600"), position("SGOV", "400")),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-denom",
            [make_order("prop-denom", 0, "SHV", Side.BUY, "300", PRICE)],
        )
        portfolio = project_portfolio(context.positions, proposal.legs, PRICES)
        assert portfolio.total_invested_value == Decimal("130000.00")
        assert portfolio.concentration_of("SHV") == Decimal("30000.00") / Decimal("130000.00")
        decision = evaluate(proposal, context)
        assert decision.result_for(CheckId.CHECK_04).passed

    def test_balanced_proposal_fails_when_existing_breach_persists(self) -> None:
        """Inverse direction: proposal-only shares (50/50) look compliant,
        but the projected denominator still shows SGOV above the limit."""
        context = make_context(
            prices=PRICES,
            positions=(position("SGOV", "800"), position("BIL", "200")),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-denom-2",
            [
                make_order("prop-denom-2", 0, "SGOV", Side.BUY, "50", PRICE),
                make_order("prop-denom-2", 1, "BIL", Side.BUY, "50", PRICE),
            ],
        )
        portfolio = project_portfolio(context.positions, proposal.legs, PRICES)
        assert portfolio.concentration_of("SGOV") == Decimal("85000.00") / Decimal("110000.00")
        decision = evaluate(proposal, context)
        assert not decision.result_for(CheckId.CHECK_04).passed

    def test_no_invested_value_is_vacuously_compliant(self) -> None:
        context = make_context(obligations=(), operating_reserve=Decimal("0"))
        proposal = make_proposal("prop-empty", [])
        decision = evaluate(proposal, context)
        assert decision.result_for(CheckId.CHECK_04).passed

    def test_exactly_at_limit_passes(self) -> None:
        context = make_context(
            prices=PRICES,
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-at-limit",
            [
                make_order("prop-at-limit", 0, "SGOV", Side.BUY, "70", PRICE),
                make_order("prop-at-limit", 1, "BIL", Side.BUY, "30", PRICE),
            ],
        )
        decision = evaluate(proposal, context)
        assert decision.result_for(CheckId.CHECK_04).passed


class TestDecisionShape:
    def test_results_follow_check_order(self) -> None:
        context = make_context()
        proposal = make_proposal(
            "prop-order",
            [make_order("prop-order", 0, "SGOV", Side.BUY, "1", "100.00")],
        )
        decision = evaluate(proposal, context)
        assert [r.check_id for r in decision.results] == list(CHECK_ORDER)
