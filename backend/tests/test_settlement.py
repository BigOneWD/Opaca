"""CHECK-12 settlement timing and derived T+1 sell settlement.

Required proofs 17 and 18 (plus 13 exercised through the engine).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from opaca.calendar.us_trading_calendar import US_TRADING_CALENDAR
from opaca.domain.models import CheckId, Obligation, Position, Proposal, Side
from opaca.treasury.liquidity import sell_settlement_events

from tests.helpers import evaluate, make_context, make_order, make_proposal

FRIDAY = date(2026, 8, 28)
FRIDAY_NOW = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)  # 10:30 EDT
SETTLES_MONDAY = date(2026, 8, 31)

PRICE = Decimal("100.00")
PRICES = {"SGOV": PRICE, "BIL": PRICE, "SHV": PRICE}


def sell_100_sgov(proposal_id: str) -> Proposal:
    return make_proposal(proposal_id, [make_order(proposal_id, 0, "SGOV", Side.SELL, "100", PRICE)])


def sgov_position() -> Position:
    return Position(
        symbol="SGOV",
        quantity=Decimal("100"),
        quantity_available=Decimal("100"),
        market_value=Decimal("10000.00"),
    )


class TestDerivedSellSettlement:
    def test_friday_sell_settles_monday(self) -> None:
        proposal = sell_100_sgov("prop-settle")
        events = sell_settlement_events(proposal.legs, FRIDAY, US_TRADING_CALENDAR)
        assert len(events) == 1
        assert events[0].trade_date == FRIDAY
        assert events[0].settlement_date == SETTLES_MONDAY
        assert events[0].amount == Decimal("10000.00")


class TestCheck12:
    def test_obligation_due_before_settlement_fails_check_12(self) -> None:
        """Required proof 17. Settled cash alone cannot fund the obligation
        and the proposed proceeds only settle Monday (T+1), so a same-day
        obligation cannot be funded."""
        from opaca.domain.models import BrokerCashState

        obligation = Obligation(
            obligation_id="tax",
            name="tax payment",
            amount=Decimal("10000.00"),
            due_date=FRIDAY,
        )
        small_broker = BrokerCashState(
            cash=Decimal("500.00"),
            buying_power=Decimal("2000.00"),
            non_marginable_buying_power=Decimal("500.00"),
            multiplier=Decimal("4"),
            as_of=FRIDAY_NOW,
        )
        context = make_context(
            prices=PRICES,
            now=FRIDAY_NOW,
            positions=(sgov_position(),),
            obligations=(obligation,),
            operating_reserve=Decimal("0"),
            broker=small_broker,
        )
        decision = evaluate(sell_100_sgov("prop-12-fail"), context)
        result = decision.result_for(CheckId.CHECK_12)
        assert not result.passed
        assert "tax" in result.detail

    def test_obligation_due_after_settlement_passes_check_12(self) -> None:
        """Required proof 18. Settled cash alone (500) cannot fund the 10k
        obligation, but the derived proceeds settle Monday 2026-08-31, before
        the Tuesday 2026-09-01 due date."""
        from opaca.domain.models import BrokerCashState

        obligation = Obligation(
            obligation_id="tax",
            name="tax payment",
            amount=Decimal("10000.00"),
            due_date=date(2026, 9, 1),
        )
        small_broker = BrokerCashState(
            cash=Decimal("500.00"),
            buying_power=Decimal("2000.00"),
            non_marginable_buying_power=Decimal("500.00"),
            multiplier=Decimal("4"),
            as_of=FRIDAY_NOW,
        )
        context = make_context(
            prices=PRICES,
            now=FRIDAY_NOW,
            positions=(sgov_position(),),
            obligations=(obligation,),
            operating_reserve=Decimal("0"),
            broker=small_broker,
        )
        proposal = sell_100_sgov("prop-12-pass")
        decision = evaluate(proposal, context)
        assert decision.result_for(CheckId.CHECK_12).passed

    def test_obligation_due_on_settlement_date_is_funded(self) -> None:
        from opaca.domain.models import BrokerCashState

        obligation = Obligation(
            obligation_id="tax",
            name="tax payment",
            amount=Decimal("10000.00"),
            due_date=SETTLES_MONDAY,
        )
        small_broker = BrokerCashState(
            cash=Decimal("500.00"),
            buying_power=Decimal("2000.00"),
            non_marginable_buying_power=Decimal("500.00"),
            multiplier=Decimal("4"),
            as_of=FRIDAY_NOW,
        )
        context = make_context(
            prices=PRICES,
            now=FRIDAY_NOW,
            positions=(sgov_position(),),
            obligations=(obligation,),
            operating_reserve=Decimal("0"),
            broker=small_broker,
        )
        assert (
            evaluate(sell_100_sgov("prop-12-sameday"), context).result_for(CheckId.CHECK_12).passed
        )

    def test_buy_only_proposal_is_vacuous_for_check_12(self) -> None:
        context = make_context(
            prices=PRICES, now=FRIDAY_NOW, obligations=(), operating_reserve=Decimal("0")
        )
        proposal = make_proposal(
            "prop-buy-only", [make_order("prop-buy-only", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        result = evaluate(proposal, context).result_for(CheckId.CHECK_12)
        assert result.passed
        assert "vacuous" in result.detail

    def test_settled_cash_alone_funding_obligation_passes_without_proceeds(self) -> None:
        obligation = Obligation(
            obligation_id="tax",
            name="tax payment",
            amount=Decimal("10000.00"),
            due_date=FRIDAY,
        )
        context = make_context(
            prices=PRICES,
            now=FRIDAY_NOW,
            positions=(sgov_position(),),
            obligations=(obligation,),
            operating_reserve=Decimal("0"),
        )
        # cash 100k covers the 10k obligation without needing sale proceeds
        assert (
            evaluate(sell_100_sgov("prop-12-covered"), context).result_for(CheckId.CHECK_12).passed
        )
