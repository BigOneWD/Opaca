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


@dataclass(frozen=True)
class LocalLedger:
    scenario: ScenarioSeed | None
    snapshot: PersistedSnapshot | None
    reservations: tuple[ReservationRecord, ...]
    unknown_orders: tuple[UnknownOrderRecord, ...]
    settlement_as_of: date
