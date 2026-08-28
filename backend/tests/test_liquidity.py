"""Settlement-aware liquidity engine.

Required proof 16: immediate paper-account sale crediting does NOT make
derived unsettled proceeds operationally available. Fixture: Phase -1B
evidence b7 (paper cash credited to $99,999.99 immediately at terminal fill).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import cast

import pytest
from opaca.domain.models import BrokerCashState, SettlementEvent
from opaca.domain.money import round_budget
from opaca.treasury.liquidity import (
    LedgerInconsistencyError,
    compute_liquidity,
)
from opaca.treasury.scenario import seed_scenario

from tests.helpers import load_evidence

B7_TRADE_DATE = date(2026, 8, 28)
B7_SETTLE_DATE = date(2026, 8, 31)  # T+1: Friday -> Monday, weekend skipped


def b7_sell_event() -> SettlementEvent:
    """Derived settlement event for the Phase -1B b7 sell:
    qty 1.199207531 SGOV filled @ 100.69 on 2026-08-28."""
    evidence = load_evidence("b7_settlement_sell_20260828T135900Z.json")
    observations = cast(dict[str, object], evidence["observations"])
    sells = cast(list[dict[str, object]], observations["sells"])
    sell = sells[0]
    final_order = cast(dict[str, object], sell["final_order"])
    qty = Decimal(str(sell["qty_sold"]))
    price = Decimal(str(final_order["filled_avg_price"]))
    return SettlementEvent(
        event_id="b7-leg-0",
        symbol=str(sell["symbol"]),
        trade_date=B7_TRADE_DATE,
        settlement_date=B7_SETTLE_DATE,
        amount=round_budget(qty * price),
    )


class TestPaperImmediateCreditIsNotOperational:
    """Required proof 16."""

    def test_unsettled_proceeds_excluded_from_settled_cash(self) -> None:
        event = b7_sell_event()
        broker_cash = Decimal("99999.99")  # paper credited immediately (evidence b7)
        projection = compute_liquidity(
            broker=_broker(broker_cash),
            obligations=(),
            settlement_events=(event,),
            operating_reserve=Decimal("0"),
            as_of=B7_TRADE_DATE,
        )
        assert event.amount == Decimal("120.74")
        assert projection.unsettled_total == Decimal("120.74")
        assert projection.settled_cash == broker_cash - Decimal("120.74")
        assert projection.settled_cash == Decimal("99879.25")
        assert projection.investable_cash == Decimal("99879.25")

    def test_proceeds_become_operational_only_on_derived_settlement_date(self) -> None:
        event = b7_sell_event()
        broker_cash = Decimal("99999.99")
        projection = compute_liquidity(
            broker=_broker(broker_cash),
            obligations=(),
            settlement_events=(event,),
            operating_reserve=Decimal("0"),
            as_of=B7_TRADE_DATE,
        )
        assert projection.available_on(B7_TRADE_DATE) == Decimal("99879.25")
        assert projection.available_on(date(2026, 8, 29)) == Decimal("99879.25")  # Sat
        assert projection.available_on(date(2026, 8, 30)) == Decimal("99879.25")  # Sun
        assert projection.available_on(B7_SETTLE_DATE) == Decimal("99999.99")

    def test_broker_ledger_is_not_modified_or_duplicated(self) -> None:
        event = b7_sell_event()
        projection = compute_liquidity(
            broker=_broker(Decimal("99999.99")),
            obligations=(),
            settlement_events=(event,),
            operating_reserve=Decimal("0"),
            as_of=B7_TRADE_DATE,
        )
        assert projection.broker_cash == Decimal("99999.99")
        assert projection.settled_cash + projection.unsettled_total == projection.broker_cash

    def test_already_settled_events_are_not_double_counted(self) -> None:
        settled_event = SettlementEvent(
            event_id="old",
            symbol="SGOV",
            trade_date=date(2026, 8, 26),
            settlement_date=date(2026, 8, 27),
            amount=Decimal("500.00"),
        )
        projection = compute_liquidity(
            broker=_broker(Decimal("1000.00")),
            obligations=(),
            settlement_events=(settled_event,),
            operating_reserve=Decimal("0"),
            as_of=B7_TRADE_DATE,
        )
        assert projection.unsettled_events == ()
        assert projection.settled_cash == Decimal("1000.00")


class TestLedgerIntegrity:
    def test_negative_derived_settled_cash_fails_closed(self) -> None:
        event = SettlementEvent(
            event_id="x",
            symbol="SGOV",
            trade_date=B7_TRADE_DATE,
            settlement_date=B7_SETTLE_DATE,
            amount=Decimal("2000.00"),
        )
        with pytest.raises(LedgerInconsistencyError):
            compute_liquidity(
                broker=_broker(Decimal("1000.00")),
                obligations=(),
                settlement_events=(event,),
                operating_reserve=Decimal("0"),
                as_of=B7_TRADE_DATE,
            )


class TestProtectedLiquidityAndInvestable:
    def test_demo_scenario_liquidity_split(self) -> None:
        seed = seed_scenario(Decimal("100000"), date(2026, 9, 1))
        projection = compute_liquidity(
            broker=_broker(Decimal("100000")),
            obligations=seed.obligations,
            settlement_events=(),
            operating_reserve=seed.operating_reserve,
            as_of=date(2026, 9, 1),
        )
        assert projection.protected_liquidity == Decimal("78000.00")
        assert projection.investable_cash == Decimal("22000.00")
        assert projection.funding_ceiling == Decimal("22000.00")

    def test_investable_cash_negative_when_cash_falls_below_protected(self) -> None:
        seed = seed_scenario(Decimal("100000"), date(2026, 9, 1))
        projection = compute_liquidity(
            broker=_broker(Decimal("70000")),
            obligations=seed.obligations,
            settlement_events=(),
            operating_reserve=seed.operating_reserve,
            as_of=date(2026, 9, 1),
        )
        assert projection.investable_cash == Decimal("-8000.00")
        assert projection.funding_ceiling == Decimal("0")

    def test_schedule_covers_all_obligation_and_settlement_dates(self) -> None:
        seed = seed_scenario(Decimal("100000"), date(2026, 9, 1))
        event = b7_sell_event()
        projection = compute_liquidity(
            broker=_broker(Decimal("100120.74")),
            obligations=seed.obligations,
            settlement_events=(event,),
            operating_reserve=seed.operating_reserve,
            as_of=B7_TRADE_DATE,
        )
        scheduled_dates = {row.on_date for row in projection.schedule}
        assert seed.obligations[0].due_date in scheduled_dates
        assert seed.obligations[1].due_date in scheduled_dates
        assert B7_SETTLE_DATE in scheduled_dates
        for row in projection.schedule:
            assert row.headroom == row.available - projection.operating_reserve


def _broker(cash: Decimal) -> BrokerCashState:
    return BrokerCashState(
        cash=cash,
        buying_power=Decimal("400000"),
        non_marginable_buying_power=cash,
        multiplier=Decimal("4"),
        as_of=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
    )
