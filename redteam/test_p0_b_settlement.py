"""P0-B: settlement false liquidity / double counting."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from helpers import (
    DEFAULT_NOW, evaluate, make_context, make_order, make_proposal,
)
from opaca.calendar.us_trading_calendar import US_TRADING_CALENDAR
from opaca.domain.models import CheckId, Obligation, Position, SettlementEvent, Side
from opaca.treasury.liquidity import LedgerInconsistencyError, compute_liquidity

TODAY = date(2026, 9, 1)          # Tuesday
SETTLES = date(2026, 9, 2)        # T+1 Wednesday


def _sold_today_event(amount="60000"):
    return SettlementEvent(
        event_id="ev-sold-today", symbol="SGOV",
        trade_date=TODAY, settlement_date=SETTLES, amount=Decimal(amount),
    )


def test_broker_cash_including_todays_proceeds_is_not_operationally_available():
    """Alpaca paper credits sale proceeds instantly. Broker cash 100k already
    contains 60k of unsettled proceeds; operational settled cash must be 40k."""
    ctx = make_context(cash="100000", settlement_events=(_sold_today_event(),))
    liq = compute_liquidity(
        broker=ctx.broker, obligations=(), settlement_events=ctx.settlement_events,
        operating_reserve=Decimal("0"), as_of=TODAY,
    )
    assert liq.broker_cash == Decimal("100000")
    assert liq.unsettled_total == Decimal("60000")
    assert liq.settled_cash == Decimal("40000")


def test_obligation_before_settlement_fails_despite_sufficient_broker_cash():
    """Obligation 80k due TODAY. Broker cash reads 100k (sufficient on its
    face) but 60k is unsettled -> proposal must fail."""
    obligation = Obligation(
        obligation_id="ob-today", name="payroll",
        amount=Decimal("80000"), due_date=TODAY,
    )
    ctx = make_context(
        cash="100000", obligations=(obligation,), operating_reserve=Decimal("0"),
        settlement_events=(_sold_today_event(),),
    )
    prop = make_proposal("p-b", [make_order("p-b", 0, "SGOV", Side.BUY, "10", "100.69")])
    decision = evaluate(prop, ctx)
    assert not decision.passed
    assert not decision.result_for(CheckId.CHECK_02).passed
    assert not decision.result_for(CheckId.CHECK_01).passed


def test_no_double_count_between_broker_cash_and_recorded_settlement_event():
    """A settlement event whose date has passed is already inside broker cash
    and must NOT be added again."""
    settled_already = SettlementEvent(
        event_id="ev-old", symbol="SGOV",
        trade_date=date(2026, 8, 27), settlement_date=date(2026, 8, 28),
        amount=Decimal("30000"),
    )
    liq = compute_liquidity(
        broker=make_context(cash="100000").broker, obligations=(),
        settlement_events=(settled_already,), operating_reserve=Decimal("0"), as_of=TODAY,
    )
    assert liq.unsettled_total == Decimal("0")
    assert liq.settled_cash == Decimal("100000")   # not 130000
    assert liq.available_on(TODAY) == Decimal("100000")


def test_no_double_count_between_recorded_event_and_proposed_sell():
    """A recorded unsettled sell plus a NEW proposed sell of the same symbol
    must contribute exactly once each, never twice."""
    position = Position(symbol="SGOV", quantity=Decimal("1000"),
                        quantity_available=Decimal("1000"), market_value=Decimal("100690"))
    obligation = Obligation(obligation_id="ob", name="payroll",
                            amount=Decimal("1"), due_date=date(2026, 9, 30))
    ctx = make_context(cash="100000", positions=(position,), obligations=(obligation,),
                       operating_reserve=Decimal("0"),
                       settlement_events=(_sold_today_event(),))
    prop = make_proposal("p-sell", [
        make_order("p-sell", 0, "SGOV", Side.SELL, "100", "100.69")  # 10,069.00
    ])
    decision = evaluate(prop, ctx)
    liq = compute_liquidity(broker=ctx.broker, obligations=ctx.obligations,
                            settlement_events=ctx.settlement_events,
                            operating_reserve=Decimal("0"), as_of=TODAY)
    # settled 40,000 + recorded 60,000 + proposed 10,069 = 110,069 total, once each
    far = date(2026, 9, 30)
    assert liq.settled_cash == Decimal("40000")
    assert liq.proceeds_settling_by(far) == Decimal("60000")
    from opaca.treasury.liquidity import sell_settlement_events
    proposed = sell_settlement_events(prop.legs, TODAY, US_TRADING_CALENDAR)
    assert sum(e.amount for e in proposed) == Decimal("10069.00")
    total = liq.settled_cash + liq.proceeds_settling_by(far) + sum(e.amount for e in proposed)
    assert total == Decimal("110069.00")
    assert decision.result_for(CheckId.CHECK_12).passed


def test_proposed_sell_proceeds_never_available_on_trade_date():
    """Proposed sell settles T+1; it must not fund an obligation due today."""
    position = Position(symbol="SGOV", quantity=Decimal("1000"),
                        quantity_available=Decimal("1000"), market_value=Decimal("100690"))
    obligation = Obligation(obligation_id="ob-now", name="payroll",
                            amount=Decimal("50000"), due_date=TODAY)
    ctx = make_context(cash="10000", positions=(position,), obligations=(obligation,),
                       operating_reserve=Decimal("0"))
    prop = make_proposal("p-liq", [
        make_order("p-liq", 0, "SGOV", Side.SELL, "600", "100.69")  # 60,414
    ])
    decision = evaluate(prop, ctx)
    assert not decision.result_for(CheckId.CHECK_12).passed, \
        "T+1 proceeds must not cover a same-day obligation"
    assert not decision.result_for(CheckId.CHECK_02).passed


def test_proposed_sell_proceeds_do_not_fund_same_proposal_buys():
    """CHECK-01/06/11 must not treat proposed sell proceeds as investable."""
    position = Position(symbol="SGOV", quantity=Decimal("1000"),
                        quantity_available=Decimal("1000"), market_value=Decimal("100690"))
    ctx = make_context(cash="1000", positions=(position,), obligations=(),
                       operating_reserve=Decimal("0"))
    prop = make_proposal("p-rot", [
        make_order("p-rot", 0, "SGOV", Side.SELL, "500", "100.69"),   # +50,345
        make_order("p-rot", 1, "BIL", Side.BUY, "500", "92.00"),      # -46,000
    ])
    decision = evaluate(prop, ctx)
    assert not decision.result_for(CheckId.CHECK_01).passed
    assert not decision.result_for(CheckId.CHECK_11).passed


def test_ledger_inconsistency_when_unsettled_exceeds_broker_cash():
    """Corrupt/contradictory ledger must raise deterministically, not permit."""
    with pytest.raises(LedgerInconsistencyError):
        compute_liquidity(
            broker=make_context(cash="10000").broker, obligations=(),
            settlement_events=(_sold_today_event("60000"),),
            operating_reserve=Decimal("0"), as_of=TODAY,
        )
