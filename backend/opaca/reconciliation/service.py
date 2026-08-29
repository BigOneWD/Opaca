"""Deterministic broker/local reconciliation. Unknown ≠ failed; uncertainty ≠ trade."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from opaca.broker.adapters import (
    ACCOUNT_REDACT_KEYS,
    UNRESOLVED_ALPACA_STATES,
    adapt_account,
    adapt_asset,
    adapt_order_snapshot,
    adapt_position,
    as_mapping,
    sanitize_account_diagnostics,
)
from opaca.broker.errors import BrokerUnavailableError, InvalidBrokerStateError
from opaca.broker.gateway import ASSET_UNIVERSE, AlpacaGateway, assert_read_only_gateway
from opaca.domain.models import AssetState, BrokerCashState, OrderState, Position, SettlementEvent
from opaca.persistence.store import SQLiteStore
from opaca.persistence.types import (
    AuditEventType,
    OrderSnapshotRecord,
    PersistedSnapshot,
    ReconciliationStatus,
    ReservationKind,
    ReservationRecord,
    ReservationStatus,
    UnknownOrderRecord,
)
from opaca.treasury.liquidity import LedgerInconsistencyError, compute_liquidity


@dataclass(frozen=True)
class ReconciliationResult:
    status: ReconciliationStatus
    reasons: tuple[str, ...]
    snapshot: PersistedSnapshot | None
    broker: BrokerCashState | None
    positions: tuple[Position, ...]
    assets: tuple[AssetState, ...]
    orders: tuple[OrderSnapshotRecord, ...]


def _diagnostics_json(account: object) -> str:
    data = dict(as_mapping(account))
    for key in ACCOUNT_REDACT_KEYS:
        data.pop(key, None)
    return json.dumps(sanitize_account_diagnostics(data), default=str, separators=(",", ":"))


def _read_broker(
    gateway: AlpacaGateway, now: datetime
) -> tuple[
    BrokerCashState,
    tuple[Position, ...],
    tuple[AssetState, ...],
    tuple[OrderSnapshotRecord, ...],
    str,
]:
    assert_read_only_gateway(gateway)
    account_raw = gateway.get_account()
    positions_raw = gateway.get_positions()
    orders_raw = gateway.get_open_orders()
    assets = tuple(adapt_asset(gateway.get_asset(symbol)) for symbol in ASSET_UNIVERSE)
    broker = adapt_account(account_raw, now)
    positions = tuple(adapt_position(item) for item in positions_raw)
    orders = tuple(adapt_order_snapshot(item) for item in orders_raw)
    return broker, positions, assets, orders, _diagnostics_json(account_raw)


def _lookup_unknown(
    gateway: AlpacaGateway,
    records: Sequence[UnknownOrderRecord],
    now: datetime,
) -> tuple[list[UnknownOrderRecord], list[str], bool]:
    """Resolve local UNKNOWN identities. Never auto-retry/submit."""
    updated: list[UnknownOrderRecord] = []
    reasons: list[str] = []
    review_required = False
    for record in records:
        if record.state == OrderState.UNKNOWN_REQUIRES_REVIEW.value:
            reasons.append(f"UNKNOWN_REQUIRES_REVIEW already set for {record.client_order_id}")
            updated.append(record)
            review_required = True
            continue
        if record.state != OrderState.UNKNOWN.value:
            updated.append(record)
            continue
        try:
            found = gateway.get_order_by_client_id(record.client_order_id)
        except BrokerUnavailableError:
            reasons.append(
                f"UNKNOWN lookup unavailable for {record.client_order_id}; review required"
            )
            updated.append(
                UnknownOrderRecord(
                    client_order_id=record.client_order_id,
                    proposal_id=record.proposal_id,
                    symbol=record.symbol,
                    side=record.side,
                    quantity=record.quantity,
                    filled_quantity=record.filled_quantity,
                    state=OrderState.UNKNOWN_REQUIRES_REVIEW.value,
                    last_lookup_at=now,
                    created_at=record.created_at,
                )
            )
            review_required = True
            continue
        if found is None:
            reasons.append(f"UNKNOWN client_order_id {record.client_order_id} not found at broker")
            updated.append(
                UnknownOrderRecord(
                    client_order_id=record.client_order_id,
                    proposal_id=record.proposal_id,
                    symbol=record.symbol,
                    side=record.side,
                    quantity=record.quantity,
                    filled_quantity=record.filled_quantity,
                    state=OrderState.UNKNOWN_REQUIRES_REVIEW.value,
                    last_lookup_at=now,
                    created_at=record.created_at,
                )
            )
            review_required = True
            continue
        adapted = adapt_order_snapshot(found)
        updated.append(
            UnknownOrderRecord(
                client_order_id=record.client_order_id,
                proposal_id=record.proposal_id,
                symbol=adapted.symbol,
                side=adapted.side,
                quantity=adapted.quantity,
                filled_quantity=adapted.filled_quantity,
                state=adapted.mapped_state,
                last_lookup_at=now,
                created_at=record.created_at,
            )
        )
        reasons.append(
            f"UNKNOWN {record.client_order_id} located at broker as {adapted.mapped_state}"
        )
    return updated, reasons, review_required


def compare_state(
    *,
    broker: BrokerCashState,
    positions: Sequence[Position],
    orders: Sequence[OrderSnapshotRecord],
    previous: PersistedSnapshot | None,
    reservations: Sequence[ReservationRecord],
    unknown_orders: Sequence[UnknownOrderRecord],
    settlement_events: Sequence[SettlementEvent],
    as_of: date,
) -> tuple[ReconciliationStatus, list[str]]:
    reasons: list[str] = []
    status = ReconciliationStatus.RECONCILED

    try:
        compute_liquidity(
            broker,
            obligations=(),
            settlement_events=settlement_events,
            operating_reserve=Decimal("0"),
            as_of=as_of,
        )
    except LedgerInconsistencyError as exc:
        reasons.append(f"broker cash inconsistent with recorded unsettled proceeds: {exc}")
        status = ReconciliationStatus.DRIFT_DETECTED

    local_client_ids = {
        reservation.client_order_id
        for reservation in reservations
        if reservation.client_order_id is not None
    }
    local_client_ids.update(record.client_order_id for record in unknown_orders)

    for order in orders:
        mapped = OrderState(order.mapped_state)
        if mapped is OrderState.UNKNOWN:
            reasons.append(f"unmapped broker order status {order.alpaca_status!r}")
            status = ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW
        if mapped in UNRESOLVED_ALPACA_STATES and order.client_order_id not in local_client_ids:
            reasons.append(f"broker unresolved order {order.client_order_id} unknown locally")
            if status is ReconciliationStatus.RECONCILED:
                status = ReconciliationStatus.DRIFT_DETECTED

    submitted_ids = {
        record.client_order_id
        for record in unknown_orders
        if record.state
        in {
            OrderState.SUBMITTED.value,
            OrderState.UNKNOWN.value,
            OrderState.UNKNOWN_REQUIRES_REVIEW.value,
        }
    }
    broker_ids = {order.client_order_id for order in orders}
    for client_order_id in submitted_ids:
        if client_order_id not in broker_ids:
            reasons.append(f"local submitted/UNKNOWN order {client_order_id} absent at broker")
            status = ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW

    if previous is not None:
        prev_qty = {position.symbol: position.quantity for position in previous.positions}
        curr_qty = {position.symbol: position.quantity for position in positions}
        if prev_qty != curr_qty:
            reasons.append("position quantity changed versus prior snapshot")
            if status is ReconciliationStatus.RECONCILED:
                status = ReconciliationStatus.DRIFT_DETECTED

    reserved_by_symbol: dict[str, Decimal] = {}
    for reservation in reservations:
        if (
            reservation.kind is ReservationKind.SELL_QUANTITY
            and reservation.status is ReservationStatus.ACTIVE
            and reservation.symbol is not None
            and reservation.quantity is not None
        ):
            reserved_by_symbol[reservation.symbol] = (
                reserved_by_symbol.get(reservation.symbol, Decimal("0")) + reservation.quantity
            )
    curr_pos = {position.symbol: position for position in positions}
    for symbol, reserved in reserved_by_symbol.items():
        position = curr_pos.get(symbol)
        if position is None:
            reasons.append(f"local SELL reservation for {symbol} but broker position missing")
            if status is ReconciliationStatus.RECONCILED:
                status = ReconciliationStatus.DRIFT_DETECTED
            continue
        broker_held_aside = position.quantity - position.quantity_available
        if broker_held_aside > reserved:
            reasons.append(f"{symbol} quantity_available inconsistent with local reservations")
            if status is ReconciliationStatus.RECONCILED:
                status = ReconciliationStatus.DRIFT_DETECTED

    if any(record.state == OrderState.UNKNOWN_REQUIRES_REVIEW.value for record in unknown_orders):
        status = ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW
        reasons.append("local UNKNOWN_REQUIRES_REVIEW blocks trading")

    return status, reasons


def reconcile(
    store: SQLiteStore,
    gateway: AlpacaGateway,
    *,
    now: datetime,
    seed_if_needed: bool = True,
) -> ReconciliationResult:
    """Read broker state, compare with local ledger, persist an auditable snapshot."""
    try:
        broker, positions, assets, orders, diagnostics = _read_broker(gateway, now)
    except BrokerUnavailableError as exc:
        store.record_audit(AuditEventType.BROKER_UNAVAILABLE, now, reason=str(exc))
        return ReconciliationResult(
            status=ReconciliationStatus.BROKER_UNAVAILABLE,
            reasons=(str(exc),),
            snapshot=None,
            broker=None,
            positions=(),
            assets=(),
            orders=(),
        )
    except InvalidBrokerStateError as exc:
        store.record_audit(AuditEventType.INVALID_BROKER_STATE, now, reason=str(exc))
        return ReconciliationResult(
            status=ReconciliationStatus.INVALID_BROKER_STATE,
            reasons=(str(exc),),
            snapshot=None,
            broker=None,
            positions=(),
            assets=(),
            orders=(),
        )

    store.record_audit(
        AuditEventType.BROKER_STATE_READ,
        now,
        reason="read-only broker snapshot captured",
        detail=json.dumps({"cash": str(broker.cash)}, separators=(",", ":")),
    )

    prior_unknown = store.load_unknown_orders()
    try:
        unknown_updates, lookup_reasons, review_required = _lookup_unknown(
            gateway, prior_unknown, now
        )
    except InvalidBrokerStateError as exc:
        store.record_audit(AuditEventType.INVALID_BROKER_STATE, now, reason=str(exc))
        return ReconciliationResult(
            status=ReconciliationStatus.INVALID_BROKER_STATE,
            reasons=(str(exc),),
            snapshot=None,
            broker=broker,
            positions=positions,
            assets=assets,
            orders=orders,
        )

    with store.begin_immediate() as conn:
        for record in unknown_updates:
            store.upsert_unknown_order(record, conn=conn)
        ledger = store.load_ledger(conn=conn)
        events = store.load_settlement_events(conn=conn)
        status, reasons = compare_state(
            broker=broker,
            positions=positions,
            orders=orders,
            previous=ledger.snapshot,
            reservations=ledger.reservations,
            unknown_orders=unknown_updates,
            settlement_events=events,
            as_of=now.date(),
        )
        reasons = lookup_reasons + reasons
        if review_required:
            status = ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW
        snapshot = store.persist_snapshot(
            broker=broker,
            positions=positions,
            assets=assets,
            orders=orders,
            status=status,
            captured_at=now,
            diagnostics=diagnostics,
            reasons=reasons,
            conn=conn,
        )
        if seed_if_needed and status is ReconciliationStatus.RECONCILED:
            store.seed_scenario_once(broker.cash, now.date(), now=now, conn=conn)
        if status is ReconciliationStatus.RECONCILED:
            event_type = AuditEventType.RECONCILIATION_COMPLETE
        elif status is ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW:
            event_type = AuditEventType.UNKNOWN_REQUIRES_REVIEW
        elif status is ReconciliationStatus.INVALID_BROKER_STATE:
            event_type = AuditEventType.INVALID_BROKER_STATE
        else:
            event_type = AuditEventType.DRIFT_DETECTED
        store.record_audit(
            event_type,
            now,
            reason="; ".join(reasons) if reasons else status.value,
            snapshot_version=snapshot.version,
            conn=conn,
        )

    return ReconciliationResult(
        status=status,
        reasons=tuple(reasons),
        snapshot=snapshot,
        broker=broker,
        positions=positions,
        assets=assets,
        orders=orders,
    )
