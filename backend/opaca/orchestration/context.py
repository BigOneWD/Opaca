"""Construct PolicyContext from persisted reconciled state. No Treasury Core changes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

from opaca.broker.adapters import UNRESOLVED_ALPACA_STATES, adapt_unresolved_order
from opaca.calendar.us_trading_calendar import US_TRADING_CALENDAR, TradingCalendar
from opaca.domain.models import (
    AssetState,
    AuthorityPolicy,
    BrokerEnvironment,
    ExecutionContext,
    InvestmentPolicy,
    LiquidityPolicy,
    Obligation,
    OrderState,
    PrecloseBlackoutConfig,
    Side,
    UnresolvedOrder,
)
from opaca.domain.money import ZERO
from opaca.persistence.store import PersistenceError, SQLiteStore
from opaca.persistence.types import (
    PersistedSnapshot,
    ReconciliationStatus,
    ReservationKind,
    ReservationRecord,
    UnknownOrderRecord,
)
from opaca.policy.engine import PolicyContext


def _policy_bool(raw: str) -> bool:
    return raw in {"1", "true", "True"}


def load_investment_policy(
    store: SQLiteStore, conn: sqlite3.Connection | None = None
) -> InvestmentPolicy:
    symbols = json.loads(store.policy_value("permitted_symbols", conn=conn))
    if not isinstance(symbols, list) or not all(isinstance(item, str) for item in symbols):
        raise PersistenceError("permitted_symbols policy is corrupt")
    return InvestmentPolicy(
        permitted_symbols=frozenset(symbols),
        concentration_max_fraction=Decimal(
            store.policy_value("concentration_max_fraction", conn=conn)
        ),
        min_trade_notional=Decimal(store.policy_value("min_trade_notional", conn=conn)),
        preclose_blackout=PrecloseBlackoutConfig(
            enabled=_policy_bool(store.policy_value("preclose_blackout_enabled", conn=conn)),
            minutes_before_close=int(store.policy_value("preclose_blackout_minutes", conn=conn)),
        ),
    )


def load_authority_policy(
    store: SQLiteStore, conn: sqlite3.Connection | None = None
) -> AuthorityPolicy:
    return AuthorityPolicy(
        per_order_notional_max=Decimal(
            store.policy_value("per_order_autonomous_notional_max", conn=conn)
        ),
        per_proposal_notional_max=Decimal(
            store.policy_value("per_proposal_aggregate_notional_max", conn=conn)
        ),
        rolling_24h_notional_max=Decimal(
            store.policy_value("rolling_24h_autonomous_notional_max", conn=conn)
        ),
        rolling_order_count_max=int(
            store.policy_value("rolling_autonomous_order_count_max", conn=conn)
        ),
        runaway_hourly_order_count_max=int(
            store.policy_value("runaway_hourly_order_count_max", conn=conn)
        ),
    )


def _sell_unresolved_from_reservations(
    reservations: tuple[ReservationRecord, ...],
) -> list[UnresolvedOrder]:
    orders: list[UnresolvedOrder] = []
    for reservation in reservations:
        if reservation.kind is not ReservationKind.SELL_QUANTITY:
            continue
        if reservation.symbol is None or reservation.quantity is None:
            continue
        client_order_id = reservation.client_order_id or f"reservation-{reservation.reservation_id}"
        orders.append(
            UnresolvedOrder(
                proposal_id=reservation.proposal_id,
                symbol=reservation.symbol,
                side=Side.SELL,
                client_order_id=client_order_id,
                state=OrderState.AUTO_AUTHORIZED,
                quantity=reservation.quantity,
                filled_quantity=ZERO,
            )
        )
    return orders


def _unresolved_from_unknown(records: tuple[UnknownOrderRecord, ...]) -> list[UnresolvedOrder]:
    orders: list[UnresolvedOrder] = []
    for record in records:
        try:
            state = OrderState(record.state)
            side = Side(record.side)
        except ValueError:
            state = OrderState.UNKNOWN_REQUIRES_REVIEW
            side = Side.SELL if record.side.upper() == "SELL" else Side.BUY
        orders.append(
            UnresolvedOrder(
                proposal_id=record.proposal_id,
                symbol=record.symbol,
                side=side,
                client_order_id=record.client_order_id,
                state=state,
                quantity=record.quantity,
                filled_quantity=record.filled_quantity,
            )
        )
    return orders


def _unresolved_from_broker_orders(
    snapshot: PersistedSnapshot,
) -> list[UnresolvedOrder]:
    orders: list[UnresolvedOrder] = []
    for record in snapshot.orders:
        mapped = OrderState(record.mapped_state)
        if mapped not in UNRESOLVED_ALPACA_STATES:
            continue
        orders.append(
            adapt_unresolved_order(record, proposal_id=f"broker:{record.client_order_id}")
        )
    return orders


def _cash_reservation_obligations(
    reservations: tuple[ReservationRecord, ...],
    as_of_date: datetime,
) -> tuple[Obligation, ...]:
    extra: list[Obligation] = []
    for reservation in reservations:
        if reservation.kind is not ReservationKind.CASH_DEPLOYMENT:
            continue
        if reservation.amount is None or reservation.amount <= ZERO:
            continue
        extra.append(
            Obligation(
                obligation_id=f"reservation-cash:{reservation.proposal_id}",
                name="reserved deployment",
                amount=reservation.amount,
                due_date=as_of_date.date(),
            )
        )
    return tuple(extra)


def build_policy_context(
    store: SQLiteStore,
    *,
    now: datetime,
    prices: Mapping[str, Decimal],
    calendar: TradingCalendar = US_TRADING_CALENDAR,
    conn: sqlite3.Connection | None = None,
    environment_verified: bool = True,
) -> tuple[PolicyContext, PersistedSnapshot]:
    snapshot = store.latest_snapshot(conn=conn)
    if snapshot is None:
        raise PersistenceError("no reconciled snapshot exists")
    if snapshot.reconciliation_status is not ReconciliationStatus.RECONCILED:
        raise PersistenceError(
            f"latest snapshot is {snapshot.reconciliation_status.value}; not tradable"
        )
    scenario = store.get_scenario(conn=conn)
    if scenario is None:
        raise PersistenceError("scenario has not been seeded")
    reservations = store.active_reservations(conn=conn)
    unknown = store.load_unknown_orders(conn=conn)
    obligations = store.load_obligations(conn=conn) + _cash_reservation_obligations(
        reservations, now
    )
    unresolved = tuple(
        _sell_unresolved_from_reservations(reservations)
        + _unresolved_from_unknown(unknown)
        + _unresolved_from_broker_orders(snapshot)
    )
    assets: dict[str, AssetState] = {asset.symbol: asset for asset in snapshot.assets}
    context = PolicyContext(
        broker=snapshot.broker,
        positions=snapshot.positions,
        obligations=obligations,
        settlement_events=store.load_settlement_events(conn=conn),
        assets=assets,
        prices=prices,
        liquidity_policy=LiquidityPolicy(operating_reserve=scenario.operating_reserve),
        investment_policy=load_investment_policy(store, conn=conn),
        authority_policy=load_authority_policy(store, conn=conn),
        execution=ExecutionContext(
            environment=BrokerEnvironment.PAPER,
            environment_verified=environment_verified,
            kill_switch_active=store.kill_switch_active(conn=conn),
            now=now,
        ),
        unresolved_orders=unresolved,
        autonomous_history=store.load_autonomous_history(conn=conn),
        calendar=calendar,
    )
    return context, snapshot
