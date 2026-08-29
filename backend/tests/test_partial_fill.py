"""Partial-fill safety modeling (SPEC s12, RT-09).

Every non-empty subset of ALL legs — buys and sells alike — is evaluated
through the applicable hard controls. Under the Amendment G fixed
investment-pool base, a single buy leg filling alone never shows a fake
100% concentration; the residual buy-side risk is funding/liquidity (a buy
filling without the sells that were meant to fund obligations). Sell-side:
zero/partial fill must never be represented as restored liquidity.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from opaca.domain.models import BrokerCashState, CheckId, Obligation, Position, Side
from opaca.policy.partial_fill import MAX_ENUMERATED_LEGS, assess_partial_fill_safety

from tests.helpers import DEFAULT_NOW, ENGINE, make_context, make_order, make_proposal

PRICE = Decimal("100.00")
PRICES = {"SGOV": PRICE, "BIL": PRICE, "SHV": PRICE}


def position(symbol: str, quantity: str) -> Position:
    qty = Decimal(quantity)
    return Position(symbol=symbol, quantity=qty, quantity_available=qty, market_value=qty * PRICE)


class TestSubsetEnumeration:
    def test_all_non_empty_subsets_are_evaluated(self) -> None:
        context = make_context(prices=PRICES, obligations=(), operating_reserve=Decimal("0"))
        two = make_proposal(
            "prop-2",
            [
                make_order("prop-2", 0, "SGOV", Side.BUY, "1", PRICE),
                make_order("prop-2", 1, "BIL", Side.BUY, "1", PRICE),
            ],
        )
        three = make_proposal(
            "prop-3",
            [
                make_order("prop-3", 0, "SGOV", Side.BUY, "1", PRICE),
                make_order("prop-3", 1, "BIL", Side.BUY, "1", PRICE),
                make_order("prop-3", 2, "SHV", Side.BUY, "1", PRICE),
            ],
        )
        assert assess_partial_fill_safety(two, context, ENGINE).subsets_evaluated == 3
        assert assess_partial_fill_safety(three, context, ENGINE).subsets_evaluated == 7

    def test_mixed_buy_sell_proposal_enumerates_every_subset(self) -> None:
        context = make_context(
            prices=PRICES,
            positions=(position("SGOV", "1000"),),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-mixed",
            [
                make_order("prop-mixed", 0, "SGOV", Side.SELL, "100", PRICE),
                make_order("prop-mixed", 1, "BIL", Side.BUY, "50", PRICE),
            ],
        )
        assessment = assess_partial_fill_safety(proposal, context, ENGINE)
        assert assessment.subsets_evaluated == 3
        assert assessment.safe

    def test_leg_count_ceiling_fails_closed(self) -> None:
        context = make_context(prices=PRICES, obligations=(), operating_reserve=Decimal("0"))
        legs = [
            make_order("prop-many", i, "SGOV", Side.BUY, "1", PRICE)
            for i in range(MAX_ENUMERATED_LEGS + 1)
        ]
        assessment = assess_partial_fill_safety(make_proposal("prop-many", legs), context, ENGINE)
        assert not assessment.safe
        assert assessment.subsets_evaluated == 0
        assert any("too many buy legs" in v for v in assessment.violations)


class TestProceedsBridgingAnObligationGap:
    def test_sell_whose_proceeds_bridge_the_gap_is_unsafe_at_zero_fill(self) -> None:
        """The settlement timing of the full liquidation is sound — the
        derived proceeds settle before the obligation is due (CHECK-02/12
        pass). With zero fills the gap reopens, so partial-fill safety is
        UNSAFE: liquidity must not be represented as restored until actual
        fills reconcile and settle."""
        obligation = Obligation(
            obligation_id="tax",
            name="tax payment",
            amount=Decimal("10000.00"),
            due_date=date(2026, 9, 8),
        )
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
            "prop-bridge", [make_order("prop-bridge", 0, "SGOV", Side.SELL, "100", PRICE)]
        )
        decision = ENGINE.evaluate(proposal, context)
        assert decision.result_for(CheckId.CHECK_02).passed
        assert decision.result_for(CheckId.CHECK_12).passed
        assessment = assess_partial_fill_safety(proposal, context, ENGINE)
        assert not assessment.safe
        assert assessment.zero_fill_covers_obligations is False
        assert any("zero-fill" in v for v in assessment.violations)

    def test_mixed_rotation_covered_without_proceeds_is_safe(self) -> None:
        """Control: when settled cash alone covers the obligation, every
        subset of a sell+buy rotation is safe (buy-subset funding is
        bounded by CHECK-01's investable cash)."""
        obligation = Obligation(
            obligation_id="tax",
            name="tax payment",
            amount=Decimal("30000.00"),
            due_date=date(2026, 9, 20),
        )
        context = make_context(
            prices=PRICES,
            positions=(position("SGOV", "1000"),),
            obligations=(obligation,),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-rotate",
            [
                make_order("prop-rotate", 0, "SGOV", Side.SELL, "500", PRICE),
                make_order("prop-rotate", 1, "BIL", Side.BUY, "400", PRICE),
            ],
        )
        decision = ENGINE.evaluate(proposal, context)
        assert decision.passed
        assessment = assess_partial_fill_safety(proposal, context, ENGINE)
        assert assessment.safe
        assert assessment.subsets_evaluated == 3


class TestSellSubsets:
    def test_sell_subsets_run_through_the_hard_controls(self) -> None:
        """RT-09: sell subsets are evaluated for concentration as well
        (CHECK-04); selling can only reduce a symbol's projected value
        against the fixed pool base, so a compliant full liquidation stays
        compliant under every subset."""
        context = make_context(
            prices=PRICES,
            positions=(position("SGOV", "600"), position("BIL", "400")),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-sells",
            [
                make_order("prop-sells", 0, "SGOV", Side.SELL, "300", PRICE),
                make_order("prop-sells", 1, "BIL", Side.SELL, "250", PRICE),
            ],
        )
        assessment = assess_partial_fill_safety(proposal, context, ENGINE)
        assert assessment.subsets_evaluated == 3
        assert assessment.safe
        assert assessment.violations == ()

    def test_zero_fill_must_not_be_represented_as_restored_liquidity(self) -> None:
        obligation = Obligation(
            obligation_id="tax",
            name="tax payment",
            amount=Decimal("10000.00"),
            due_date=date(2026, 9, 8),
        )
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
        assert assessment.safe
