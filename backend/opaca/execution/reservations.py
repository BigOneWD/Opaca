"""Reservation resize/release against proven broker disposition."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

from opaca.domain.models import Side
from opaca.domain.money import ZERO, round_budget
from opaca.execution.states import TERMINAL_STATES
from opaca.persistence.store import SQLiteStore
from opaca.persistence.types import ExecutionState, ReservationKind


def sync_proposal_reservations(
    store: SQLiteStore,
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    now: datetime,
) -> None:
    """Resize or release reservations from proven fills/terminal state.

    UNKNOWN and SUBMITTING retain capacity. A timeout is not a release.
    """
    orders = store.list_execution_orders(conn=conn, proposal_id=proposal_id)
    if not orders:
        return
    if any(
        item.state in {ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION, ExecutionState.SUBMITTING}
        for item in orders
    ):
        return
    legs = store.load_proposal_legs(proposal_id, conn=conn)
    executed = {item.client_order_id for item in orders}
    sell_remaining: dict[str, Decimal] = {}
    buy_remaining_notional = ZERO
    for item in orders:
        remainder = ZERO if item.state in TERMINAL_STATES else item.remaining_quantity
        if item.side is Side.SELL:
            sell_remaining[item.symbol] = sell_remaining.get(item.symbol, ZERO) + remainder
        else:
            buy_remaining_notional += round_budget(remainder * item.reference_price)
    for leg in legs:
        if leg.client_order_id in executed:
            continue
        if leg.side is Side.SELL:
            sell_remaining[leg.symbol] = sell_remaining.get(leg.symbol, ZERO) + leg.quantity
        else:
            buy_remaining_notional += leg.notional
    for symbol, remaining in sell_remaining.items():
        if remaining <= ZERO:
            store.release_active_reservations(
                proposal_id=proposal_id,
                now=now,
                conn=conn,
                kinds=(ReservationKind.SELL_QUANTITY,),
                symbol=symbol,
            )
        else:
            store.resize_active_reservation(
                proposal_id=proposal_id,
                kind=ReservationKind.SELL_QUANTITY,
                now=now,
                quantity=remaining,
                symbol=symbol,
                conn=conn,
            )
    if buy_remaining_notional <= ZERO:
        store.release_active_reservations(
            proposal_id=proposal_id,
            now=now,
            conn=conn,
            kinds=(ReservationKind.CASH_DEPLOYMENT,),
        )
    else:
        store.resize_active_reservation(
            proposal_id=proposal_id,
            kind=ReservationKind.CASH_DEPLOYMENT,
            now=now,
            amount=buy_remaining_notional,
            conn=conn,
        )
    for item in orders:
        if item.state in TERMINAL_STATES:
            store.release_active_reservations(
                proposal_id=proposal_id,
                now=now,
                conn=conn,
                kinds=(ReservationKind.ORDER_IDENTITY,),
                client_order_id=item.client_order_id,
            )
