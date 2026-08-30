"""Persistence-layer enumerations and record types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from opaca.domain.models import (
    AssetState,
    AuthorityResult,
    BrokerCashState,
    Position,
    Side,
)
from opaca.treasury.scenario import ScenarioSeed


class ReservationKind(StrEnum):
    SELL_QUANTITY = "SELL_QUANTITY"
    CASH_DEPLOYMENT = "CASH_DEPLOYMENT"
    ORDER_IDENTITY = "ORDER_IDENTITY"


class ReservationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"


class ProposalRecordStatus(StrEnum):
    REJECTED = "REJECTED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    AUTO_AUTHORIZED = "AUTO_AUTHORIZED"


class AuditEventType(StrEnum):
    BROKER_STATE_READ = "BROKER_STATE_READ"
    RECONCILIATION_COMPLETE = "RECONCILIATION_COMPLETE"
    DRIFT_DETECTED = "DRIFT_DETECTED"
    PROPOSAL_EVALUATED = "PROPOSAL_EVALUATED"
    POLICY_REJECTED = "POLICY_REJECTED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    RESERVATION_CREATED = "RESERVATION_CREATED"
    RESERVATION_DENIED = "RESERVATION_DENIED"
    UNKNOWN_REQUIRES_REVIEW = "UNKNOWN_REQUIRES_REVIEW"
    SCENARIO_SEEDED = "SCENARIO_SEEDED"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    INVALID_BROKER_STATE = "INVALID_BROKER_STATE"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"
    EXECUTION_REVALIDATED = "EXECUTION_REVALIDATED"
    SUBMISSION_INTENT_CREATED = "SUBMISSION_INTENT_CREATED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_ACKNOWLEDGED = "ORDER_ACKNOWLEDGED"
    ORDER_UNKNOWN = "ORDER_UNKNOWN"
    ORDER_RECOVERED = "ORDER_RECOVERED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FULL_FILL = "FULL_FILL"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_NOT_SUBMITTED = "ORDER_NOT_SUBMITTED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    RESERVATION_RESIZED = "RESERVATION_RESIZED"
    RESERVATION_RELEASED = "RESERVATION_RELEASED"
    SETTLEMENT_CREATED = "SETTLEMENT_CREATED"
    SETTLEMENT_COMPLETED = "SETTLEMENT_COMPLETED"
    HUMAN_APPROVAL_GRANTED = "HUMAN_APPROVAL_GRANTED"
    EXECUTION_BLOCKED = "EXECUTION_BLOCKED"


class ReconciliationStatus(StrEnum):
    RECONCILED = "RECONCILED"
    DRIFT_DETECTED = "DRIFT_DETECTED"
    UNKNOWN_REQUIRES_REVIEW = "UNKNOWN_REQUIRES_REVIEW"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    INVALID_BROKER_STATE = "INVALID_BROKER_STATE"


@dataclass(frozen=True)
class AuditEvent:
    event_type: AuditEventType
    timestamp: datetime
    proposal_id: str | None
    snapshot_version: int | None
    reason: str
    detail: str
    broker_identifiers: str


@dataclass(frozen=True)
class ReservationRecord:
    reservation_id: int
    proposal_id: str
    kind: ReservationKind
    symbol: str | None
    quantity: Decimal | None
    amount: Decimal | None
    client_order_id: str | None
    leg_index: int | None
    status: ReservationStatus
    created_at: datetime


@dataclass(frozen=True)
class OrderSnapshotRecord:
    client_order_id: str
    broker_order_id: str | None
    symbol: str
    side: str
    alpaca_status: str
    mapped_state: str
    quantity: Decimal | None
    filled_quantity: Decimal | None


@dataclass(frozen=True)
class PersistedSnapshot:
    snapshot_id: int
    version: int
    broker: BrokerCashState
    positions: tuple[Position, ...]
    assets: tuple[AssetState, ...]
    orders: tuple[OrderSnapshotRecord, ...]
    reconciliation_status: ReconciliationStatus
    captured_at: datetime
    diagnostics: str


@dataclass(frozen=True)
class ProposalRecord:
    proposal_id: str
    proposal_hash: str
    status: ProposalRecordStatus
    authority_result: AuthorityResult
    snapshot_version: int
    created_at: datetime
    expires_at: datetime | None
    source_snapshot_id: int | None

    def is_currently_valid_approval(self, now: datetime) -> bool:
        """Expired approval is not a valid approval. Exact expiry is expired.

        This is not execution authority. A future submit path must still
        re-reconcile, rebuild PolicyContext, and re-run TreasuryGuard.
        Human approval never overrides a hard failure.
        """
        if self.status is not ProposalRecordStatus.APPROVAL_REQUIRED:
            return False
        if self.authority_result is not AuthorityResult.APPROVAL_REQUIRED:
            return False
        if self.expires_at is None:
            return False
        return now < self.expires_at


@dataclass(frozen=True)
class UnknownOrderRecord:
    client_order_id: str
    proposal_id: str
    symbol: str
    side: str
    quantity: Decimal | None
    filled_quantity: Decimal | None
    state: str
    last_lookup_at: datetime | None
    created_at: datetime


class ExecutionState(StrEnum):
    READY = "READY"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    NOT_SUBMITTED = "NOT_SUBMITTED"
    UNKNOWN_REQUIRES_RECONCILIATION = "UNKNOWN_REQUIRES_RECONCILIATION"


@dataclass(frozen=True)
class ExecutionOrderRecord:
    client_order_id: str
    proposal_id: str
    leg_index: int
    symbol: str
    side: Side
    quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    state: ExecutionState
    broker_order_id: str | None
    last_broker_status: str | None
    filled_avg_price: Decimal | None
    reference_price: Decimal
    reconciled_filled_quantity: Decimal
    settled_proceeds: Decimal
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class LocalLedger:
    scenario: ScenarioSeed | None
    snapshot: PersistedSnapshot | None
    reservations: tuple[ReservationRecord, ...]
    unknown_orders: tuple[UnknownOrderRecord, ...]
    settlement_as_of: date
    execution_orders: tuple[ExecutionOrderRecord, ...] = ()
