"""Paper execution lifecycle.

evaluate → reserve → revalidate → submit → ack/UNKNOWN → fill →
reconcile reservation → settlement → audit.

Broker I/O is outside BEGIN IMMEDIATE. Unknown ≠ failed. Uncertainty never
creates a second order.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from opaca.broker.adapters import adapt_order_snapshot
from opaca.broker.errors import BrokerUnavailableError, PaperEnvironmentError
from opaca.broker.gateway import (
    LIVE_ENDPOINT,
    PAPER_ENDPOINT,
    AlpacaGateway,
    assert_read_only_gateway,
)
from opaca.calendar.us_trading_calendar import US_TRADING_CALENDAR, TradingCalendar
from opaca.domain.models import (
    AuthorityResult,
    OrderState,
    Proposal,
    ProposedOrder,
    SettlementEvent,
    Side,
)
from opaca.domain.money import ZERO, non_negative_money, round_budget
from opaca.execution.errors import DuplicateSubmissionError, ExecutionBlockedError
from opaca.execution.gateway import (
    DuplicateClientOrderIdError,
    PaperMutatingGateway,
    PaperOrderRequest,
    assert_paper_execution_gateway,
)
from opaca.execution.reservations import sync_proposal_reservations
from opaca.execution.states import (
    OPEN_RECOVERY_STATES,
    TERMINAL_STATES,
    map_broker_status,
    validate_transition,
)
from opaca.orchestration.context import build_policy_context
from opaca.orchestration.reserve import proposal_hash
from opaca.persistence.store import PersistenceError, SQLiteStore
from opaca.persistence.types import (
    AuditEventType,
    ExecutionOrderRecord,
    ExecutionState,
    ProposalRecordStatus,
    ReconciliationStatus,
    ReservationKind,
    ReservationStatus,
    UnknownOrderRecord,
)
from opaca.policy.decision import decide
from opaca.reconciliation.service import reconcile

_SNAPSHOT_MAX_AGE_REASON = "stale snapshot"


@dataclass(frozen=True)
class ExecutionResult:
    proposal_id: str
    client_order_ids: tuple[str, ...]
    state: ExecutionState
    filled_quantity: Decimal
    remaining_quantity: Decimal
    blocked: bool
    block_reason: str | None
    recovered: bool
    submitted: bool
    snapshot_version: int | None
    reservation_active: bool


def grant_human_approval(
    store: SQLiteStore,
    proposal: Proposal,
    *,
    now: datetime,
) -> None:
    """Record a proposal-bound, hash-bound approval. Does not submit."""
    digest = proposal_hash(proposal)
    with store.begin_immediate() as conn:
        existing = store.get_proposal(proposal.proposal_id, conn=conn)
        if existing is None:
            raise ExecutionBlockedError("proposal does not exist")
        if existing.proposal_hash != digest:
            raise ExecutionBlockedError("approval payload_hash does not match proposal")
        if not existing.is_currently_valid_approval(now):
            raise ExecutionBlockedError("approval expired")
        if store.get_approval_grant(proposal.proposal_id, conn=conn) is not None:
            return
        store.grant_human_approval(
            proposal_id=proposal.proposal_id,
            payload_hash=digest,
            now=now,
            conn=conn,
        )


def execute_reserved_proposal(
    store: SQLiteStore,
    read_gateway: AlpacaGateway,
    mutate_gateway: PaperMutatingGateway,
    proposal: Proposal,
    *,
    now: datetime,
    prices: Mapping[str, Decimal],
    calendar: TradingCalendar = US_TRADING_CALENDAR,
    environment_verified: bool = True,
) -> ExecutionResult:
    """Fresh recon + TreasuryGuard + authority, then at most one submit per leg."""
    assert_read_only_gateway(read_gateway)
    assert_paper_execution_gateway(mutate_gateway)
    _forbid_live_endpoint(mutate_gateway)

    existing = store.list_execution_orders(proposal_id=proposal.proposal_id)
    if existing:
        return recover_proposal(
            store,
            read_gateway,
            proposal.proposal_id,
            now=now,
            calendar=calendar,
        )

    recon = reconcile(store, read_gateway, now=now)
    if recon.status is not ReconciliationStatus.RECONCILED or recon.snapshot is None:
        reason = f"reconciliation {recon.status.value}"
        _audit_blocked(store, proposal.proposal_id, now, reason, None)
        return _blocked_result(proposal, reason, None)

    try:
        intent_error = _persist_submission_intents(
            store,
            proposal,
            now=now,
            prices=prices,
            calendar=calendar,
            environment_verified=environment_verified,
            expected_snapshot_version=recon.snapshot.version,
        )
    except DuplicateSubmissionError:
        return recover_proposal(
            store,
            read_gateway,
            proposal.proposal_id,
            now=now,
            calendar=calendar,
        )
    if intent_error is not None:
        return _blocked_result(proposal, intent_error, recon.snapshot.version)

    last: ExecutionOrderRecord | None = None
    submitted = False
    for leg in proposal.legs:
        last = _submit_leg(
            store,
            read_gateway,
            mutate_gateway,
            proposal,
            leg,
            now=now,
            calendar=calendar,
        )
        submitted = True
        if last.state in {
            ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION,
            ExecutionState.REJECTED,
        }:
            break
        last = _sync_leg_from_broker(
            store, read_gateway, last.client_order_id, now=now, calendar=calendar
        )
    assert last is not None
    active = _has_active_capacity(store, proposal.proposal_id)
    return ExecutionResult(
        proposal_id=proposal.proposal_id,
        client_order_ids=tuple(leg.client_order_id for leg in proposal.legs),
        state=last.state,
        filled_quantity=last.filled_quantity,
        remaining_quantity=last.remaining_quantity,
        blocked=False,
        block_reason=None,
        recovered=False,
        submitted=submitted,
        snapshot_version=recon.snapshot.version,
        reservation_active=active,
    )


def recover_proposal(
    store: SQLiteStore,
    read_gateway: AlpacaGateway,
    proposal_id: str,
    *,
    now: datetime,
    calendar: TradingCalendar = US_TRADING_CALENDAR,
) -> ExecutionResult:
    """Lookup by client_order_id. Never resubmit."""
    assert_read_only_gateway(read_gateway)
    orders = store.list_execution_orders(proposal_id=proposal_id)
    if not orders:
        raise ExecutionBlockedError(f"no execution rows for {proposal_id}")
    last = orders[-1]
    for item in orders:
        if item.state in OPEN_RECOVERY_STATES:
            last = _sync_leg_from_broker(
                store, read_gateway, item.client_order_id, now=now, calendar=calendar
            )
    active = _has_active_capacity(store, proposal_id)
    return ExecutionResult(
        proposal_id=proposal_id,
        client_order_ids=tuple(item.client_order_id for item in orders),
        state=last.state,
        filled_quantity=last.filled_quantity,
        remaining_quantity=last.remaining_quantity,
        blocked=last.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION,
        block_reason=(
            "UNKNOWN requires reconciliation"
            if last.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
            else None
        ),
        recovered=True,
        submitted=False,
        snapshot_version=None,
        reservation_active=active,
    )


def recover_open_executions(
    store: SQLiteStore,
    read_gateway: AlpacaGateway,
    *,
    now: datetime,
    calendar: TradingCalendar = US_TRADING_CALENDAR,
) -> tuple[ExecutionResult, ...]:
    """Restart recovery: reconcile every non-terminal execution by client_order_id."""
    open_orders = store.list_execution_orders(states=tuple(OPEN_RECOVERY_STATES))
    seen: list[str] = []
    results: list[ExecutionResult] = []
    for item in open_orders:
        if item.proposal_id in seen:
            continue
        seen.append(item.proposal_id)
        results.append(
            recover_proposal(store, read_gateway, item.proposal_id, now=now, calendar=calendar)
        )
    return tuple(results)


def cancel_remaining(
    store: SQLiteStore,
    read_gateway: AlpacaGateway,
    mutate_gateway: PaperMutatingGateway,
    client_order_id: str,
    *,
    now: datetime,
    calendar: TradingCalendar = US_TRADING_CALENDAR,
) -> ExecutionResult:
    """Cancel a known broker order. UNKNOWN cannot be cancelled until recovered."""
    assert_read_only_gateway(read_gateway)
    assert_paper_execution_gateway(mutate_gateway)
    record = store.get_execution_order(client_order_id)
    if record is None:
        raise ExecutionBlockedError("execution order does not exist")
    if record.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION:
        raise ExecutionBlockedError("UNKNOWN cannot be cancelled until recovered")
    if record.state is ExecutionState.SUBMITTING:
        raise ExecutionBlockedError("SUBMITTING cannot be cancelled; recover first")
    if record.state in TERMINAL_STATES:
        return recover_proposal(store, read_gateway, record.proposal_id, now=now, calendar=calendar)
    if record.broker_order_id is None:
        raise ExecutionBlockedError("no broker_order_id; recover before cancel")
    with store.begin_immediate() as conn:
        current = store.get_execution_order(client_order_id, conn=conn)
        if current is None:
            raise ExecutionBlockedError("execution order disappeared")
        validate_transition(current.state, ExecutionState.CANCEL_PENDING)
        updated = replace(current, state=ExecutionState.CANCEL_PENDING, updated_at=now)
        store.update_execution_order(updated, conn=conn)
    try:
        mutate_gateway.cancel_order_by_id(record.broker_order_id)
    except BrokerUnavailableError:
        with store.begin_immediate() as conn:
            current = store.get_execution_order(client_order_id, conn=conn)
            if current is None:
                raise ExecutionBlockedError("execution order disappeared") from None
            validate_transition(current.state, ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION)
            unknown = replace(
                current,
                state=ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION,
                updated_at=now,
            )
            store.update_execution_order(unknown, conn=conn)
            _upsert_unknown(store, conn, unknown, now)
            store.record_audit(
                AuditEventType.ORDER_UNKNOWN,
                now,
                proposal_id=unknown.proposal_id,
                reason="cancel response lost",
                broker_identifiers=unknown.client_order_id,
                conn=conn,
            )
        return recover_proposal(store, read_gateway, record.proposal_id, now=now, calendar=calendar)
    synced = _sync_leg_from_broker(store, read_gateway, client_order_id, now=now, calendar=calendar)
    active = _has_active_capacity(store, record.proposal_id)
    return ExecutionResult(
        proposal_id=record.proposal_id,
        client_order_ids=(client_order_id,),
        state=synced.state,
        filled_quantity=synced.filled_quantity,
        remaining_quantity=synced.remaining_quantity,
        blocked=False,
        block_reason=None,
        recovered=False,
        submitted=False,
        snapshot_version=None,
        reservation_active=active,
    )


def _forbid_live_endpoint(gateway: PaperMutatingGateway) -> None:
    endpoint = gateway.endpoint
    if endpoint.startswith(LIVE_ENDPOINT):
        raise PaperEnvironmentError("live Alpaca endpoint is forbidden")
    if not endpoint.startswith(PAPER_ENDPOINT):
        raise PaperEnvironmentError("paper endpoint not confirmed")


def _persist_submission_intents(
    store: SQLiteStore,
    proposal: Proposal,
    *,
    now: datetime,
    prices: Mapping[str, Decimal],
    calendar: TradingCalendar,
    environment_verified: bool,
    expected_snapshot_version: int,
) -> str | None:
    digest = proposal_hash(proposal)
    with store.begin_immediate() as conn:
        existing_exec = store.list_execution_orders(conn=conn, proposal_id=proposal.proposal_id)
        if existing_exec:
            raise DuplicateSubmissionError(proposal.proposal_id)
        record = store.get_proposal(proposal.proposal_id, conn=conn)
        if record is None:
            reason = "proposal has not been reserved"
            _audit_blocked_conn(store, conn, proposal.proposal_id, now, reason, None)
            return reason
        if record.proposal_hash != digest:
            reason = "proposal_id reused with a different payload"
            _audit_blocked_conn(
                store, conn, proposal.proposal_id, now, reason, record.snapshot_version
            )
            return reason
        snapshot = store.latest_snapshot(conn=conn)
        if snapshot is None:
            reason = "no reconciled snapshot"
            _audit_blocked_conn(store, conn, proposal.proposal_id, now, reason, None)
            return reason
        if snapshot.reconciliation_status is not ReconciliationStatus.RECONCILED:
            reason = f"latest snapshot is {snapshot.reconciliation_status.value}"
            _audit_blocked_conn(store, conn, proposal.proposal_id, now, reason, snapshot.version)
            return reason
        if expected_snapshot_version != snapshot.version:
            reason = _SNAPSHOT_MAX_AGE_REASON
            store.record_audit(
                AuditEventType.STALE_SNAPSHOT,
                now,
                proposal_id=proposal.proposal_id,
                snapshot_version=snapshot.version,
                reason=reason,
                conn=conn,
            )
            return reason
        max_age = int(store.policy_value("max_snapshot_age_seconds", conn=conn))
        if snapshot.captured_at.tzinfo is None or snapshot.captured_at > now:
            reason = "snapshot captured_at is invalid"
            _audit_blocked_conn(store, conn, proposal.proposal_id, now, reason, snapshot.version)
            return reason
        if (now - snapshot.captured_at).total_seconds() > max_age:
            store.record_audit(
                AuditEventType.STALE_SNAPSHOT,
                now,
                proposal_id=proposal.proposal_id,
                snapshot_version=snapshot.version,
                reason=_SNAPSHOT_MAX_AGE_REASON,
                conn=conn,
            )
            return _SNAPSHOT_MAX_AGE_REASON
        if store.kill_switch_active(conn=conn):
            reason = "kill switch active"
            _audit_blocked_conn(store, conn, proposal.proposal_id, now, reason, snapshot.version)
            return reason
        context, snapshot = build_policy_context(
            store,
            now=now,
            prices=prices,
            calendar=calendar,
            conn=conn,
            environment_verified=environment_verified,
            excluding_proposal_id=proposal.proposal_id,
        )
        decision = decide(proposal, context)
        store.record_audit(
            AuditEventType.EXECUTION_REVALIDATED,
            now,
            proposal_id=proposal.proposal_id,
            snapshot_version=snapshot.version,
            reason=decision.result.value,
            conn=conn,
        )
        if decision.result is AuthorityResult.REJECT:
            reason = "; ".join(decision.reasons) or "policy rejected"
            _audit_blocked_conn(store, conn, proposal.proposal_id, now, reason, snapshot.version)
            return reason
        if decision.result is AuthorityResult.APPROVAL_REQUIRED:
            grant = store.get_approval_grant(proposal.proposal_id, conn=conn)
            if grant is None:
                reason = "approval required and not granted"
                _audit_blocked_conn(
                    store, conn, proposal.proposal_id, now, reason, snapshot.version
                )
                return reason
            grant_hash, _granted_at = grant
            if grant_hash != digest or not record.is_currently_valid_approval(now):
                reason = "approval expired or hash mismatch"
                _audit_blocked_conn(
                    store, conn, proposal.proposal_id, now, reason, snapshot.version
                )
                return reason
        reservations = [
            item
            for item in store.active_reservations(conn=conn)
            if item.proposal_id == proposal.proposal_id
            and item.kind in {ReservationKind.SELL_QUANTITY, ReservationKind.CASH_DEPLOYMENT}
        ]
        if not reservations:
            if record.status is ProposalRecordStatus.AUTO_AUTHORIZED:
                reason = "AUTO reservation missing"
                _audit_blocked_conn(
                    store, conn, proposal.proposal_id, now, reason, snapshot.version
                )
                return reason
            store.persist_reservations(proposal=proposal, now=now, conn=conn)
        for leg in proposal.legs:
            validate_transition(ExecutionState.READY, ExecutionState.SUBMITTING)
            intent = ExecutionOrderRecord(
                client_order_id=leg.client_order_id,
                proposal_id=proposal.proposal_id,
                leg_index=leg.leg_index,
                symbol=leg.symbol,
                side=leg.side,
                quantity=leg.quantity,
                filled_quantity=ZERO,
                remaining_quantity=leg.quantity,
                state=ExecutionState.SUBMITTING,
                broker_order_id=None,
                last_broker_status=None,
                filled_avg_price=None,
                reference_price=leg.reference_price,
                reconciled_filled_quantity=ZERO,
                settled_proceeds=ZERO,
                created_at=now,
                updated_at=now,
            )
            try:
                store.insert_execution_order(intent, conn=conn)
            except sqlite3.IntegrityError as exc:
                raise DuplicateSubmissionError(proposal.proposal_id) from exc
            store.record_audit(
                AuditEventType.SUBMISSION_INTENT_CREATED,
                now,
                proposal_id=proposal.proposal_id,
                snapshot_version=snapshot.version,
                reason="submission intent persisted before broker mutation",
                broker_identifiers=leg.client_order_id,
                conn=conn,
            )
    return None


def _submit_leg(
    store: SQLiteStore,
    read_gateway: AlpacaGateway,
    mutate_gateway: PaperMutatingGateway,
    proposal: Proposal,
    leg: ProposedOrder,
    *,
    now: datetime,
    calendar: TradingCalendar,
) -> ExecutionOrderRecord:
    request = PaperOrderRequest(
        symbol=leg.symbol,
        side=leg.side,
        quantity=leg.quantity,
        client_order_id=leg.client_order_id,
    )
    try:
        payload = mutate_gateway.submit_order(request)
    except DuplicateClientOrderIdError:
        store.record_audit(
            AuditEventType.ORDER_RECOVERED,
            now,
            proposal_id=proposal.proposal_id,
            reason="duplicate client_order_id; lookup instead of second submit",
            broker_identifiers=leg.client_order_id,
        )
        return _sync_leg_from_broker(
            store, read_gateway, leg.client_order_id, now=now, calendar=calendar
        )
    except BrokerUnavailableError as exc:
        with store.begin_immediate() as conn:
            current = store.get_execution_order(leg.client_order_id, conn=conn)
            if current is None:
                raise PersistenceError("submission intent missing") from exc
            validate_transition(current.state, ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION)
            unknown = replace(
                current,
                state=ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION,
                updated_at=now,
            )
            store.update_execution_order(unknown, conn=conn)
            _upsert_unknown(store, conn, unknown, now)
            store.record_audit(
                AuditEventType.ORDER_UNKNOWN,
                now,
                proposal_id=proposal.proposal_id,
                reason=str(exc),
                broker_identifiers=leg.client_order_id,
                conn=conn,
            )
        return unknown
    return _acknowledge_payload(store, leg.client_order_id, payload, now=now, calendar=calendar)


def _acknowledge_payload(
    store: SQLiteStore,
    client_order_id: str,
    payload: Mapping[str, object],
    *,
    now: datetime,
    calendar: TradingCalendar,
) -> ExecutionOrderRecord:
    adapted = adapt_order_snapshot(payload)
    with store.begin_immediate() as conn:
        current = store.get_execution_order(client_order_id, conn=conn)
        if current is None:
            raise PersistenceError("submission intent missing")
        target = map_broker_status(adapted.alpaca_status, filled_quantity=adapted.filled_quantity)
        if current.state is ExecutionState.SUBMITTING and target is ExecutionState.FILLED:
            validate_transition(current.state, ExecutionState.SUBMITTED)
            mid = replace(
                current,
                state=ExecutionState.SUBMITTED,
                broker_order_id=adapted.broker_order_id,
                last_broker_status=adapted.alpaca_status,
                updated_at=now,
            )
            store.update_execution_order(mid, conn=conn)
            store.record_audit(
                AuditEventType.ORDER_SUBMITTED,
                now,
                proposal_id=current.proposal_id,
                reason="broker accepted",
                broker_identifiers=client_order_id,
                conn=conn,
            )
            store.record_audit(
                AuditEventType.ORDER_ACKNOWLEDGED,
                now,
                proposal_id=current.proposal_id,
                reason=adapted.alpaca_status,
                broker_identifiers=client_order_id,
                conn=conn,
            )
            current = mid
        validate_transition(current.state, target)
        filled = adapted.filled_quantity if adapted.filled_quantity is not None else ZERO
        remaining = non_negative_money(current.quantity - filled)
        avg = _avg_price(payload, current.reference_price, filled)
        updated = replace(
            current,
            state=target,
            broker_order_id=adapted.broker_order_id or current.broker_order_id,
            last_broker_status=adapted.alpaca_status,
            filled_quantity=filled,
            remaining_quantity=remaining,
            filled_avg_price=avg,
            updated_at=now,
        )
        store.update_execution_order(updated, conn=conn)
        if target is ExecutionState.REJECTED:
            store.record_audit(
                AuditEventType.ORDER_REJECTED,
                now,
                proposal_id=updated.proposal_id,
                reason=adapted.alpaca_status,
                broker_identifiers=client_order_id,
                conn=conn,
            )
        elif current.state is ExecutionState.SUBMITTING:
            store.record_audit(
                AuditEventType.ORDER_SUBMITTED,
                now,
                proposal_id=updated.proposal_id,
                reason="broker accepted",
                broker_identifiers=client_order_id,
                conn=conn,
            )
            store.record_audit(
                AuditEventType.ORDER_ACKNOWLEDGED,
                now,
                proposal_id=updated.proposal_id,
                reason=adapted.alpaca_status,
                broker_identifiers=client_order_id,
                conn=conn,
            )
        _apply_fill_side_effects(
            store,
            conn,
            updated,
            previous_filled=current.filled_quantity,
            now=now,
            calendar=calendar,
        )
        return updated


def _sync_leg_from_broker(
    store: SQLiteStore,
    read_gateway: AlpacaGateway,
    client_order_id: str,
    *,
    now: datetime,
    calendar: TradingCalendar,
) -> ExecutionOrderRecord:
    current = store.get_execution_order(client_order_id)
    if current is None:
        raise PersistenceError("execution order missing")
    try:
        found = read_gateway.get_order_by_client_id(client_order_id)
    except BrokerUnavailableError:
        with store.begin_immediate() as conn:
            latest = store.get_execution_order(client_order_id, conn=conn)
            if latest is None:
                raise PersistenceError("execution order missing") from None
            if latest.state is not ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION:
                validate_transition(latest.state, ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION)
            unknown = replace(
                latest,
                state=ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION,
                updated_at=now,
            )
            store.update_execution_order(unknown, conn=conn)
            _upsert_unknown(store, conn, unknown, now)
            store.record_audit(
                AuditEventType.ORDER_UNKNOWN,
                now,
                proposal_id=unknown.proposal_id,
                reason="order lookup unavailable",
                broker_identifiers=client_order_id,
                conn=conn,
            )
        return unknown
    if found is None:
        with store.begin_immediate() as conn:
            latest = store.get_execution_order(client_order_id, conn=conn)
            if latest is None:
                raise PersistenceError("execution order missing")
            if latest.state in TERMINAL_STATES:
                return latest
            if latest.state is not ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION:
                validate_transition(latest.state, ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION)
            unknown = replace(
                latest,
                state=ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION,
                updated_at=now,
            )
            store.update_execution_order(unknown, conn=conn)
            _upsert_unknown(store, conn, unknown, now)
            store.record_audit(
                AuditEventType.ORDER_UNKNOWN,
                now,
                proposal_id=unknown.proposal_id,
                reason="client_order_id not found at broker",
                broker_identifiers=client_order_id,
                conn=conn,
            )
        return unknown
    adapted = adapt_order_snapshot(found)
    with store.begin_immediate() as conn:
        latest = store.get_execution_order(client_order_id, conn=conn)
        if latest is None:
            raise PersistenceError("execution order missing")
        target = map_broker_status(adapted.alpaca_status, filled_quantity=adapted.filled_quantity)
        recovered = latest.state in {
            ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION,
            ExecutionState.SUBMITTING,
        }
        if latest.state is ExecutionState.SUBMITTING and target is ExecutionState.FILLED:
            validate_transition(latest.state, ExecutionState.SUBMITTED)
            latest = replace(
                latest,
                state=ExecutionState.SUBMITTED,
                broker_order_id=adapted.broker_order_id or latest.broker_order_id,
                last_broker_status=adapted.alpaca_status,
                updated_at=now,
            )
            store.update_execution_order(latest, conn=conn)
        if latest.state is not target:
            validate_transition(latest.state, target)
        filled = (
            adapted.filled_quantity
            if adapted.filled_quantity is not None
            else latest.filled_quantity
        )
        remaining = non_negative_money(latest.quantity - filled)
        avg = _avg_price(found, latest.reference_price, filled)
        updated = replace(
            latest,
            state=target,
            broker_order_id=adapted.broker_order_id or latest.broker_order_id,
            last_broker_status=adapted.alpaca_status,
            filled_quantity=filled,
            remaining_quantity=remaining,
            filled_avg_price=avg,
            updated_at=now,
        )
        store.update_execution_order(updated, conn=conn)
        if recovered:
            store.record_audit(
                AuditEventType.ORDER_RECOVERED,
                now,
                proposal_id=updated.proposal_id,
                reason=f"recovered as {target.value}",
                broker_identifiers=client_order_id,
                conn=conn,
            )
        _apply_fill_side_effects(
            store, conn, updated, previous_filled=latest.filled_quantity, now=now, calendar=calendar
        )
        return updated


def _apply_fill_side_effects(
    store: SQLiteStore,
    conn: sqlite3.Connection,
    order: ExecutionOrderRecord,
    *,
    previous_filled: Decimal,
    now: datetime,
    calendar: TradingCalendar,
) -> None:
    if order.filled_quantity > previous_filled:
        if order.filled_quantity >= order.quantity:
            store.record_audit(
                AuditEventType.FULL_FILL,
                now,
                proposal_id=order.proposal_id,
                reason=f"filled {format(order.filled_quantity, 'f')}",
                broker_identifiers=order.client_order_id,
                conn=conn,
            )
        else:
            store.record_audit(
                AuditEventType.PARTIAL_FILL,
                now,
                proposal_id=order.proposal_id,
                reason=(
                    f"filled {format(order.filled_quantity, 'f')} "
                    f"remaining {format(order.remaining_quantity, 'f')}"
                ),
                broker_identifiers=order.client_order_id,
                conn=conn,
            )
        _record_sell_settlement(store, conn, order, now=now, calendar=calendar)
    if order.state is ExecutionState.CANCELLED:
        store.record_audit(
            AuditEventType.ORDER_CANCELLED,
            now,
            proposal_id=order.proposal_id,
            reason="broker cancelled",
            broker_identifiers=order.client_order_id,
            conn=conn,
        )
    sync_proposal_reservations(store, conn, proposal_id=order.proposal_id, now=now)
    _recognize_settlements(store, conn, order.proposal_id, now=now)


def _record_sell_settlement(
    store: SQLiteStore,
    conn: sqlite3.Connection,
    order: ExecutionOrderRecord,
    *,
    now: datetime,
    calendar: TradingCalendar,
) -> None:
    if order.side is not Side.SELL or order.filled_quantity <= ZERO:
        return
    price = order.filled_avg_price if order.filled_avg_price is not None else order.reference_price
    new_proceeds = round_budget(order.filled_quantity * price)
    increment = new_proceeds - order.settled_proceeds
    if increment <= ZERO:
        return
    event_id = f"{order.client_order_id}:fill:{format(order.filled_quantity, 'f')}"
    if store.settlement_event_exists(event_id, conn=conn):
        return
    trade_date = now.date()
    event = SettlementEvent(
        event_id=event_id,
        symbol=order.symbol,
        trade_date=trade_date,
        settlement_date=calendar.settlement_date(trade_date),
        amount=increment,
    )
    store.insert_settlement_event(event, now=now, conn=conn)
    updated = replace(order, settled_proceeds=new_proceeds)
    store.update_execution_order(updated, conn=conn)
    store.record_audit(
        AuditEventType.SETTLEMENT_CREATED,
        now,
        proposal_id=order.proposal_id,
        reason=f"T+1 proceeds {format(increment, 'f')} settle {event.settlement_date.isoformat()}",
        broker_identifiers=order.client_order_id,
        conn=conn,
    )


def _recognize_settlements(
    store: SQLiteStore,
    conn: sqlite3.Connection,
    proposal_id: str,
    *,
    now: datetime,
) -> None:
    orders = store.list_execution_orders(conn=conn, proposal_id=proposal_id)
    prefixes = tuple(item.client_order_id for item in orders)
    already = {
        event.detail
        for event in store.list_audit(
            event_type=AuditEventType.SETTLEMENT_COMPLETED, proposal_id=proposal_id, conn=conn
        )
    }
    for event in store.load_settlement_events(conn=conn):
        if not any(event.event_id.startswith(prefix) for prefix in prefixes):
            continue
        if event.settlement_date > now.date():
            continue
        if event.event_id in already:
            continue
        store.record_audit(
            AuditEventType.SETTLEMENT_COMPLETED,
            now,
            proposal_id=proposal_id,
            reason=f"proceeds available on {event.settlement_date.isoformat()}",
            detail=event.event_id,
            conn=conn,
        )


def _avg_price(payload: Mapping[str, object], fallback: Decimal, filled: Decimal) -> Decimal | None:
    if filled <= ZERO:
        return None
    raw = payload.get("filled_avg_price")
    if raw is None:
        return fallback
    return non_negative_money(Decimal(str(raw)))


def _upsert_unknown(
    store: SQLiteStore,
    conn: sqlite3.Connection,
    order: ExecutionOrderRecord,
    now: datetime,
) -> None:
    store.upsert_unknown_order(
        UnknownOrderRecord(
            client_order_id=order.client_order_id,
            proposal_id=order.proposal_id,
            symbol=order.symbol,
            side=order.side.value,
            quantity=order.quantity,
            filled_quantity=order.filled_quantity,
            state=OrderState.UNKNOWN.value,
            last_lookup_at=now,
            created_at=order.created_at,
        ),
        conn=conn,
    )


def _has_active_capacity(store: SQLiteStore, proposal_id: str) -> bool:
    return any(
        item.proposal_id == proposal_id
        and item.status is ReservationStatus.ACTIVE
        and item.kind in {ReservationKind.SELL_QUANTITY, ReservationKind.CASH_DEPLOYMENT}
        for item in store.active_reservations()
    )


def _audit_blocked(
    store: SQLiteStore,
    proposal_id: str,
    now: datetime,
    reason: str,
    snapshot_version: int | None,
) -> None:
    store.record_audit(
        AuditEventType.EXECUTION_BLOCKED,
        now,
        proposal_id=proposal_id,
        snapshot_version=snapshot_version,
        reason=reason,
    )


def _audit_blocked_conn(
    store: SQLiteStore,
    conn: sqlite3.Connection,
    proposal_id: str,
    now: datetime,
    reason: str,
    snapshot_version: int | None,
) -> None:
    store.record_audit(
        AuditEventType.EXECUTION_BLOCKED,
        now,
        proposal_id=proposal_id,
        snapshot_version=snapshot_version,
        reason=reason,
        conn=conn,
    )


def _blocked_result(
    proposal: Proposal, reason: str, snapshot_version: int | None
) -> ExecutionResult:
    return ExecutionResult(
        proposal_id=proposal.proposal_id,
        client_order_ids=tuple(leg.client_order_id for leg in proposal.legs),
        state=ExecutionState.READY,
        filled_quantity=ZERO,
        remaining_quantity=sum((leg.quantity for leg in proposal.legs), ZERO),
        blocked=True,
        block_reason=reason,
        recovered=False,
        submitted=False,
        snapshot_version=snapshot_version,
        reservation_active=True,
    )
