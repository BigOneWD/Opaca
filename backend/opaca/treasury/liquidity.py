"""Settlement-aware liquidity engine (SPEC s5, Amendment B).

Single-ledger invariant: broker cash is the only authoritative balance. The
projection below never claims cash the broker has not reconciled; it derives
an availability schedule OVER reconciled cash:

* Alpaca paper trading credits sale proceeds instantly (Phase -1B, B7),
  which is unrealistic versus real T+1 settlement;
* therefore operational settled cash = broker cash MINUS proceeds whose
  derived settlement date has not yet been reached;
* unsettled proceeds become operational only on the derived settlement date
  (T+1 on the US business-day calendar, weekend/holiday rolls included).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from opaca.calendar.us_trading_calendar import TradingCalendar
from opaca.domain.models import (
    BrokerCashState,
    Obligation,
    Position,
    ProposedOrder,
    SettlementEvent,
    Side,
)
from opaca.domain.money import ZERO, round_budget, round_money


class LedgerInconsistencyError(ValueError):
    """Raised when derived availability contradicts the reconciled ledger."""


@dataclass(frozen=True)
class DateLiquidity:
    on_date: date
    proceeds_settled_cumulative: Decimal
    obligations_due_cumulative: Decimal
    available: Decimal
    headroom: Decimal


@dataclass(frozen=True)
class LiquidityProjection:
    as_of: date
    broker_cash: Decimal
    unsettled_events: tuple[SettlementEvent, ...]
    unsettled_total: Decimal
    settled_cash: Decimal
    operating_reserve: Decimal
    obligations: tuple[Obligation, ...]
    obligations_total: Decimal
    protected_liquidity: Decimal
    investable_cash: Decimal
    funding_ceiling: Decimal
    schedule: tuple[DateLiquidity, ...]

    def proceeds_settling_by(self, day: date) -> Decimal:
        return sum((e.amount for e in self.unsettled_events if e.settlement_date <= day), ZERO)

    def obligations_due_by(self, day: date) -> Decimal:
        return sum((o.amount for o in self.obligations if o.due_date <= day), ZERO)

    def available_on(self, day: date) -> Decimal:
        return self.settled_cash + self.proceeds_settling_by(day) - self.obligations_due_by(day)

    def headroom_on(self, day: date) -> Decimal:
        return self.available_on(day) - self.operating_reserve


def compute_liquidity(
    broker: BrokerCashState,
    obligations: Sequence[Obligation],
    settlement_events: Sequence[SettlementEvent],
    operating_reserve: Decimal,
    as_of: date,
) -> LiquidityProjection:
    """Derive settlement-aware liquidity from reconciled broker cash.

    Events whose settlement date has already passed are reflected in broker
    cash and are not double-counted; events settling after ``as_of`` are
    excluded from operational settled cash until their settlement date.
    """
    reserve = round_money(operating_reserve)
    unsettled = tuple(
        sorted(
            (e for e in settlement_events if e.settlement_date > as_of),
            key=lambda e: (e.settlement_date, e.event_id),
        )
    )
    unsettled_total = sum((e.amount for e in unsettled), ZERO)
    settled_cash = broker.cash - unsettled_total
    if settled_cash < ZERO:
        raise LedgerInconsistencyError(
            "derived settled cash is negative: broker cash does not cover "
            "recorded unsettled proceeds"
        )
    obligations_tuple = tuple(obligations)
    obligations_total = sum((o.amount for o in obligations_tuple), ZERO)
    protected = reserve + obligations_total
    investable = settled_cash - protected
    ceiling = investable if investable > ZERO else ZERO

    relevant_dates = sorted(
        {as_of} | {o.due_date for o in obligations_tuple} | {e.settlement_date for e in unsettled}
    )
    schedule = tuple(
        DateLiquidity(
            on_date=day,
            proceeds_settled_cumulative=sum(
                (e.amount for e in unsettled if e.settlement_date <= day), ZERO
            ),
            obligations_due_cumulative=sum(
                (o.amount for o in obligations_tuple if o.due_date <= day), ZERO
            ),
            available=(
                settled_cash
                + sum((e.amount for e in unsettled if e.settlement_date <= day), ZERO)
                - sum((o.amount for o in obligations_tuple if o.due_date <= day), ZERO)
            ),
            headroom=(
                settled_cash
                + sum((e.amount for e in unsettled if e.settlement_date <= day), ZERO)
                - sum((o.amount for o in obligations_tuple if o.due_date <= day), ZERO)
                - reserve
            ),
        )
        for day in relevant_dates
    )
    return LiquidityProjection(
        as_of=as_of,
        broker_cash=broker.cash,
        unsettled_events=unsettled,
        unsettled_total=unsettled_total,
        settled_cash=settled_cash,
        operating_reserve=reserve,
        obligations=obligations_tuple,
        obligations_total=obligations_total,
        protected_liquidity=protected,
        investable_cash=investable,
        funding_ceiling=ceiling,
        schedule=schedule,
    )


class MissingPriceError(KeyError):
    """Raised when a symbol involved in the projection has no reference
    price; policy must fail closed rather than guess."""


@dataclass(frozen=True)
class ProjectedPosition:
    symbol: str
    existing_quantity: Decimal
    proposed_delta: Decimal
    projected_quantity: Decimal
    reference_price: Decimal
    projected_market_value: Decimal


@dataclass(frozen=True)
class PortfolioProjection:
    """Projected post-trade portfolio state (SPEC s9 CHECK-04, Amendment G).

    ``investment_pool_base`` is the denominator used for every
    concentration fraction in ``concentration_by_symbol``; when no explicit
    pool base is supplied it degenerates to the projected invested market
    value (standalone projection use only — the policy engine always passes
    the proposal-fixed INVESTMENT POOL BASE).
    """

    positions: tuple[ProjectedPosition, ...]
    total_invested_value: Decimal
    concentration_by_symbol: Mapping[str, Decimal]
    investment_pool_base: Decimal

    def concentration_of(self, symbol: str) -> Decimal:
        return self.concentration_by_symbol.get(symbol, ZERO)


def investment_pool_base(
    positions: Sequence[Position],
    prices: Mapping[str, Decimal],
    investable_cash: Decimal,
) -> Decimal:
    """INVESTMENT POOL BASE (SPEC s9 CHECK-04, Amendment G; red-team RT-02).

    The concentration denominator is the investment-pool capital, fixed at
    proposal evaluation time:

        current market value of eligible investment holdings
        + current deployable investment cash

    Deployable investment cash is the settlement-aware investable cash:
    reconciled settled cash minus protected reserve and cash committed to
    obligations. Total corporate cash is NOT part of this denominator, and
    neither is any non-deployable cash. Negative investable cash is a
    liquidity event, not deployable capital, and contributes zero.
    """
    holdings_value = ZERO
    for position in positions:
        if position.symbol not in prices:
            raise MissingPriceError(position.symbol)
        holdings_value += round_money(position.quantity * prices[position.symbol])
    deployable = investable_cash if investable_cash > ZERO else ZERO
    return holdings_value + deployable


def project_portfolio(
    positions: Sequence[Position],
    orders: Sequence[ProposedOrder],
    prices: Mapping[str, Decimal],
    pool_base: Decimal | None = None,
) -> PortfolioProjection:
    existing = {p.symbol: p.quantity for p in positions}
    delta: dict[str, Decimal] = {}
    for order in orders:
        signed = order.quantity if order.side is Side.BUY else -order.quantity
        delta[order.symbol] = delta.get(order.symbol, ZERO) + signed

    symbols = sorted(set(existing) | set(delta))
    projected: list[ProjectedPosition] = []
    total = ZERO
    for symbol in symbols:
        if symbol not in prices:
            raise MissingPriceError(symbol)
        price = prices[symbol]
        projected_quantity = existing.get(symbol, ZERO) + delta.get(symbol, ZERO)
        market_value = (
            round_money(projected_quantity * price) if projected_quantity > ZERO else ZERO
        )
        projected.append(
            ProjectedPosition(
                symbol=symbol,
                existing_quantity=existing.get(symbol, ZERO),
                proposed_delta=delta.get(symbol, ZERO),
                projected_quantity=projected_quantity,
                reference_price=price,
                projected_market_value=market_value,
            )
        )
        total += market_value

    denominator = pool_base if pool_base is not None else total
    concentration: dict[str, Decimal] = {}
    for position in projected:
        if position.projected_market_value > ZERO and denominator > ZERO:
            concentration[position.symbol] = position.projected_market_value / denominator
    return PortfolioProjection(
        positions=tuple(projected),
        total_invested_value=total,
        concentration_by_symbol=concentration,
        investment_pool_base=denominator,
    )


def sell_settlement_events(
    proposal_legs: Sequence[ProposedOrder],
    trade_date: date,
    calendar: TradingCalendar,
) -> tuple[SettlementEvent, ...]:
    """Derived T+1 settlement events for proposed sells (Amendment B)."""
    if not any(leg.side is Side.SELL for leg in proposal_legs):
        return ()
    settlement = calendar.settlement_date(trade_date)
    events = []
    for leg in proposal_legs:
        if leg.side is not Side.SELL:
            continue
        events.append(
            SettlementEvent(
                event_id=f"{leg.proposal_id}:{leg.leg_index}",
                symbol=leg.symbol,
                trade_date=trade_date,
                settlement_date=settlement,
                amount=round_budget(leg.quantity * leg.reference_price),
            )
        )
    return tuple(events)
