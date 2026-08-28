"""Partial-fill safety modeling (SPEC s12).

Required proof 19: a multi-leg buy must not become concentration-
noncompliant under any filled subset. Sell-side: zero/partial fill must
never be represented as restored liquidity.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from opaca.domain.models import CheckId, Obligation, Position, Side
from opaca.policy.partial_fill import assess_partial_fill_safety

from tests.helpers import ENGINE, make_context, make_order, make_proposal

PRICE = Decimal("100.00")
PRICES = {"SGOV": PRICE, "BIL": PRICE, "SHV": PRICE}


def position(symbol: str, quantity: str) -> Position:
    qty = Decimal(quantity)
    return Position(symbol=symbol, quantity=qty, quantity_available=qty, market_value=qty * PRICE)


class TestBuyPartialFillConcentration:
    def test_subset_fill_cannot_create_prohibited_concentration(self) -> None:
        """Required proof 19. Full fill is compliant (60/40), but the SGOV
        leg filling alone would be 100% concentration."""
        context = make_context(prices=PRICES, obligations=(), operating_reserve=Decimal("0"))
        proposal = make_proposal(
            "prop-pf",
            [
                make_order("prop-pf", 0, "SGOV", Side.BUY, "150", PRICE),
                make_order("prop-pf", 1, "BIL", Side.BUY, "100", PRICE),
            ],
        )
        decision = ENGINE.evaluate(proposal, context)
        assert decision.result_for(CheckId.CHECK_04).passed
        assessment = assess_partial_fill_safety(proposal, context, ENGINE)
        assert not assessment.safe
        assert assessment.subsets_evaluated == 3
        assert any("CHECK-04" in v for v in assessment.violations)
        assert any("0:SGOV" in v for v in assessment.violations)

    def test_existing_holdings_can_make_single_leg_fills_safe(self) -> None:
        context = make_context(
            prices=PRICES,
            positions=(position("BIL", "150"), position("SHV", "150")),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-pf-safe",
            [
                make_order("prop-pf-safe", 0, "SGOV", Side.BUY, "100", PRICE),
                make_order("prop-pf-safe", 1, "BIL", Side.BUY, "50", PRICE),
            ],
        )
        assessment = assess_partial_fill_safety(proposal, context, ENGINE)
        assert assessment.safe
        assert assessment.violations == ()
        assert assessment.subsets_evaluated == 3

    def test_single_leg_proposal_evaluates_one_subset(self) -> None:
        context = make_context(
            prices=PRICES,
            positions=(position("BIL", "150"), position("SHV", "150")),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-pf-single", [make_order("prop-pf-single", 0, "SGOV", Side.BUY, "100", PRICE)]
        )
        assessment = assess_partial_fill_safety(proposal, context, ENGINE)
        assert assessment.subsets_evaluated == 1
        assert assessment.safe


class TestSellZeroFillSafety:
    def test_zero_fill_must_not_be_represented_as_restored_liquidity(self) -> None:
        obligation = Obligation(
            obligation_id="tax",
            name="tax payment",
            amount=Decimal("10000.00"),
            due_date=date(2026, 9, 8),
        )
        from opaca.domain.models import BrokerCashState

        from tests.helpers import DEFAULT_NOW

        small_broker = BrokerCashState(
            cash=Decimal("500.00"),
            buying_power=Decimal("2000.00"),
            non_marginable_buying_power=Decimal("500.00"),
            multiplier=Decimal("4"),
            as_of=DEFAULT_NOW,
        )
        context = make_context(
            prices=PRICES,
            positions=(position("SGOV", "100"),),
            obligations=(obligation,),
            operating_reserve=Decimal("0"),
            broker=small_broker,
        )
        proposal = make_proposal(
            "prop-sell-fund", [make_order("prop-sell-fund", 0, "SGOV", Side.SELL, "100", PRICE)]
        )
        assessment = assess_partial_fill_safety(proposal, context, ENGINE)
        assert assessment.zero_fill_covers_obligations is False
        assert not assessment.safe
        assert any("zero-fill" in v for v in assessment.violations)

    def test_zero_fill_covers_when_settled_cash_alone_suffices(self) -> None:
        obligation = Obligation(
            obligation_id="tax",
            name="tax payment",
            amount=Decimal("10000.00"),
            due_date=date(2026, 9, 8),
        )
        context = make_context(
            prices=PRICES,
            positions=(position("SGOV", "100"),),
            obligations=(obligation,),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-sell-extra", [make_order("prop-sell-extra", 0, "SGOV", Side.SELL, "100", PRICE)]
        )
        assessment = assess_partial_fill_safety(proposal, context, ENGINE)
        assert assessment.zero_fill_covers_obligations is True
        assert assessment.safe

    def test_buy_only_proposal_reports_no_zero_fill_dimension(self) -> None:
        context = make_context(
            prices=PRICES,
            obligations=(),
            operating_reserve=Decimal("0"),
            positions=(position("BIL", "150"), position("SHV", "150")),
        )
        proposal = make_proposal(
            "prop-buys", [make_order("prop-buys", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        assessment = assess_partial_fill_safety(proposal, context, ENGINE)
        assert assessment.zero_fill_covers_obligations is None
