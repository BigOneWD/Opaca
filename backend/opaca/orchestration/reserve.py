"""Atomic evaluate → reserve → persist. No broker submission."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from opaca.broker.errors import InvalidBrokerStateError
from opaca.calendar.us_trading_calendar import US_TRADING_CALENDAR, TradingCalendar
from opaca.domain.models import AuthorityDecision, AuthorityResult, Proposal
from opaca.orchestration.context import build_policy_context
from opaca.persistence.store import PersistenceError, SQLiteStore, StaleSnapshotError
from opaca.persistence.types import (
    AuditEventType,
    PersistedSnapshot,
    ProposalRecord,
    ProposalRecordStatus,
    ReconciliationStatus,
)
from opaca.policy.decision import decide


def proposal_hash(proposal: Proposal) -> str:
    canonical = json.dumps(
        {
            "proposal_id": proposal.proposal_id,
            "legs": [
                {
                    "leg_index": leg.leg_index,
                    "symbol": leg.symbol,
                    "side": leg.side.value,
                    "quantity": format(leg.quantity, "f"),
                    "reference_price": format(leg.reference_price, "f"),
                    "client_order_id": leg.client_order_id,
                }
                for leg in proposal.legs
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class OrchestrationResult:
    proposal_id: str
    proposal_hash: str
    authority_result: AuthorityResult | None
    reserved: bool
    blocked: bool
    block_reason: str | None
    snapshot_version: int | None
    idempotent_replay: bool
    decision: AuthorityDecision | None
    expires_at: datetime | None = None

    @property
    def is_auto(self) -> bool:
        """Currently eligible to proceed through the next execution gate.

        Historical AUTO is not current execution eligibility. Idempotent replay
        never asserts ``is_auto=True``; Phase 2 does not re-run TreasuryGuard
        on replay. A later execution path must re-evaluate against current
        state before eligibility may be asserted.

        REPLAYED / HISTORICAL AUTO != CURRENTLY EXECUTABLE AUTO
        """
        if self.idempotent_replay:
            return False
        return self.authority_result is AuthorityResult.AUTO and self.reserved and not self.blocked

    def approval_currently_valid(self, now: datetime) -> bool:
        """Expired approval is not a valid approval. Exact expiry is expired."""
        if self.authority_result is not AuthorityResult.APPROVAL_REQUIRED:
            return False
        if self.blocked:
            return False
        if self.expires_at is None:
            return False
        return now < self.expires_at


def _status_for(result: AuthorityResult) -> ProposalRecordStatus:
    if result is AuthorityResult.AUTO:
        return ProposalRecordStatus.AUTO_AUTHORIZED
    if result is AuthorityResult.APPROVAL_REQUIRED:
        return ProposalRecordStatus.APPROVAL_REQUIRED
    return ProposalRecordStatus.REJECTED


def _audit_for(result: AuthorityResult) -> AuditEventType:
    if result is AuthorityResult.AUTO:
        return AuditEventType.RESERVATION_CREATED
    if result is AuthorityResult.APPROVAL_REQUIRED:
        return AuditEventType.APPROVAL_REQUIRED
    return AuditEventType.POLICY_REJECTED


def _blocked(
    *,
    proposal_id: str,
    digest: str,
    reason: str,
    snapshot_version: int | None,
    idempotent_replay: bool,
    authority_result: AuthorityResult | None = None,
    expires_at: datetime | None = None,
) -> OrchestrationResult:
    return OrchestrationResult(
        proposal_id=proposal_id,
        proposal_hash=digest,
        authority_result=authority_result,
        reserved=False,
        blocked=True,
        block_reason=reason,
        snapshot_version=snapshot_version,
        idempotent_replay=idempotent_replay,
        decision=None,
        expires_at=expires_at,
    )


def _snapshot_gate(
    store: SQLiteStore,
    conn: sqlite3.Connection,
    *,
    now: datetime,
    expected_snapshot_version: int | None,
    snapshot: PersistedSnapshot | None,
) -> tuple[str | None, AuditEventType]:
    """Current safety gates that must hold before any executable reservation.

    Returns (block_reason, audit_type). reason is None when the snapshot is
    currently usable for AUTO.
    """
    if snapshot is None:
        return "no reconciled snapshot", AuditEventType.RESERVATION_DENIED
    if snapshot.reconciliation_status is not ReconciliationStatus.RECONCILED:
        reason = (
            f"latest snapshot is {snapshot.reconciliation_status.value}; "
            "uncertainty must never create a trade"
        )
        return reason, AuditEventType.RESERVATION_DENIED
    if expected_snapshot_version is None:
        return "expected_snapshot_version is required", AuditEventType.RESERVATION_DENIED
    if expected_snapshot_version != snapshot.version:
        return (
            "stale snapshot",
            AuditEventType.STALE_SNAPSHOT,
        )
    captured = snapshot.captured_at
    if captured.tzinfo is None:
        return "snapshot captured_at is naive", AuditEventType.RESERVATION_DENIED
    if captured > now:
        return "snapshot captured_at is in the future", AuditEventType.RESERVATION_DENIED
    max_age_seconds = int(store.policy_value("max_snapshot_age_seconds", conn=conn))
    if now - captured > timedelta(seconds=max_age_seconds):
        return "stale snapshot", AuditEventType.STALE_SNAPSHOT
    return None, AuditEventType.RESERVATION_DENIED


def _stale_audit_reason(
    expected_snapshot_version: int | None,
    snapshot: PersistedSnapshot,
    now: datetime,
    store: SQLiteStore,
    conn: sqlite3.Connection,
) -> str:
    if expected_snapshot_version is not None and expected_snapshot_version != snapshot.version:
        return f"stale snapshot {expected_snapshot_version} vs current {snapshot.version}"
    max_age_seconds = int(store.policy_value("max_snapshot_age_seconds", conn=conn))
    age = now - snapshot.captured_at
    return (
        f"stale snapshot age {age.total_seconds()}s exceeds max {max_age_seconds}s "
        f"(version {snapshot.version})"
    )


def evaluate_and_reserve(
    store: SQLiteStore,
    proposal: Proposal,
    *,
    now: datetime,
    prices: Mapping[str, Decimal],
    expected_snapshot_version: int | None = None,
    calendar: TradingCalendar = US_TRADING_CALENDAR,
    environment_verified: bool = True,
) -> OrchestrationResult:
    """BEGIN IMMEDIATE: load state, evaluate, reserve AUTO capacity, persist.

    Broker execution is NOT implemented. A later approval path must re-reconcile
    and re-run TreasuryGuard before any submission.

    ``is_auto=True`` means the proposal is *currently* eligible to proceed
    through the next execution gate after a fresh evaluation of current
    state. Idempotent replay of a historically AUTO proposal preserves
    prior authority/reservation metadata and consumes no additional
    capacity, but never itself asserts currently executable AUTO.

    Before any future execution:

        fresh broker reconciliation
        → latest snapshot/version
        → TreasuryGuard re-run
        → authority re-run
        → reservation validation/rebinding as required
        → only then may execution eligibility be asserted.
    """
    digest = proposal_hash(proposal)
    try:
        with store.begin_immediate() as conn:
            existing = store.get_proposal(proposal.proposal_id, conn=conn)
            if existing is not None and existing.proposal_hash != digest:
                store.record_audit(
                    AuditEventType.RESERVATION_DENIED,
                    now,
                    proposal_id=proposal.proposal_id,
                    snapshot_version=existing.snapshot_version,
                    reason="proposal_id reused with a different payload",
                    conn=conn,
                )
                return _blocked(
                    proposal_id=proposal.proposal_id,
                    digest=digest,
                    reason="proposal_id reused with a different payload",
                    snapshot_version=existing.snapshot_version,
                    idempotent_replay=False,
                )

            snapshot = store.latest_snapshot(conn=conn)
            gate_reason, gate_audit = _snapshot_gate(
                store,
                conn,
                now=now,
                expected_snapshot_version=expected_snapshot_version,
                snapshot=snapshot,
            )
            snapshot_version = None if snapshot is None else snapshot.version

            if existing is not None:
                return _replay_existing(
                    store,
                    conn,
                    proposal=proposal,
                    digest=digest,
                    existing=existing,
                    snapshot=snapshot,
                    snapshot_version=snapshot_version,
                    now=now,
                    gate_reason=gate_reason,
                    gate_audit=gate_audit,
                    expected_snapshot_version=expected_snapshot_version,
                )

            if gate_reason is not None:
                audit_reason = gate_reason
                if gate_audit is AuditEventType.STALE_SNAPSHOT and snapshot is not None:
                    audit_reason = _stale_audit_reason(
                        expected_snapshot_version, snapshot, now, store, conn
                    )
                store.record_audit(
                    gate_audit,
                    now,
                    proposal_id=proposal.proposal_id,
                    snapshot_version=snapshot_version,
                    reason=audit_reason,
                    conn=conn,
                )
                return _blocked(
                    proposal_id=proposal.proposal_id,
                    digest=digest,
                    reason=gate_reason,
                    snapshot_version=snapshot_version,
                    idempotent_replay=False,
                )

            context, snapshot = build_policy_context(
                store,
                now=now,
                prices=prices,
                calendar=calendar,
                conn=conn,
                environment_verified=environment_verified,
            )
            decision = decide(proposal, context)
            store.record_audit(
                AuditEventType.PROPOSAL_EVALUATED,
                now,
                proposal_id=proposal.proposal_id,
                snapshot_version=snapshot.version,
                reason=decision.result.value,
                detail=json.dumps(list(decision.reasons), separators=(",", ":")),
                conn=conn,
            )

            expiry_seconds = int(store.policy_value("approval_expiry_seconds", conn=conn))
            expires_at = now + timedelta(seconds=expiry_seconds)
            status = _status_for(decision.result)
            approval_expires = (
                expires_at if decision.result is AuthorityResult.APPROVAL_REQUIRED else None
            )
            store.persist_proposal_decision(
                proposal=proposal,
                proposal_hash=digest,
                status=status,
                decision=decision,
                snapshot=snapshot,
                now=now,
                expires_at=approval_expires,
                conn=conn,
            )

            reserved = False
            if decision.result is AuthorityResult.AUTO:
                store.persist_reservations(proposal=proposal, now=now, conn=conn)
                reserved = True
            elif decision.result is AuthorityResult.REJECT:
                store.record_audit(
                    AuditEventType.RESERVATION_DENIED,
                    now,
                    proposal_id=proposal.proposal_id,
                    snapshot_version=snapshot.version,
                    reason="; ".join(decision.reasons) or "policy rejected",
                    conn=conn,
                )

            store.record_audit(
                _audit_for(decision.result),
                now,
                proposal_id=proposal.proposal_id,
                snapshot_version=snapshot.version,
                reason=decision.result.value,
                conn=conn,
            )
            return OrchestrationResult(
                proposal_id=proposal.proposal_id,
                proposal_hash=digest,
                authority_result=decision.result,
                reserved=reserved,
                blocked=False,
                block_reason=None,
                snapshot_version=snapshot.version,
                idempotent_replay=False,
                decision=decision,
                expires_at=approval_expires,
            )
    except StaleSnapshotError as exc:
        return _blocked(
            proposal_id=proposal.proposal_id,
            digest=digest,
            reason=str(exc),
            snapshot_version=None,
            idempotent_replay=False,
        )
    except InvalidBrokerStateError as exc:
        return _blocked(
            proposal_id=proposal.proposal_id,
            digest=digest,
            reason=str(exc),
            snapshot_version=None,
            idempotent_replay=False,
        )


def _replay_existing(
    store: SQLiteStore,
    conn: sqlite3.Connection,
    *,
    proposal: Proposal,
    digest: str,
    existing: ProposalRecord,
    snapshot: PersistedSnapshot | None,
    snapshot_version: int | None,
    now: datetime,
    gate_reason: str | None,
    gate_audit: AuditEventType,
    expected_snapshot_version: int | None,
) -> OrchestrationResult:
    """Idempotent replay: no second reservation, no new authority consumption.

    Preserves historical authority/reservation metadata. Current safety
    gates still fail closed. Replay never asserts current execution
    eligibility; Phase 2 does not re-run TreasuryGuard here.
    """
    if gate_reason is not None:
        audit_reason = gate_reason
        if gate_audit is AuditEventType.STALE_SNAPSHOT and snapshot is not None:
            audit_reason = _stale_audit_reason(
                expected_snapshot_version, snapshot, now, store, conn
            )
        store.record_audit(
            gate_audit,
            now,
            proposal_id=proposal.proposal_id,
            snapshot_version=snapshot_version,
            reason=audit_reason,
            conn=conn,
        )
        return _blocked(
            proposal_id=proposal.proposal_id,
            digest=digest,
            reason=gate_reason,
            snapshot_version=snapshot_version,
            idempotent_replay=True,
            authority_result=existing.authority_result,
            expires_at=existing.expires_at,
        )

    if store.kill_switch_active(conn=conn):
        reason = "kill switch active"
        store.record_audit(
            AuditEventType.RESERVATION_DENIED,
            now,
            proposal_id=proposal.proposal_id,
            snapshot_version=snapshot_version,
            reason=reason,
            conn=conn,
        )
        return _blocked(
            proposal_id=proposal.proposal_id,
            digest=digest,
            reason=reason,
            snapshot_version=snapshot_version,
            idempotent_replay=True,
            authority_result=existing.authority_result,
            expires_at=existing.expires_at,
        )

    if existing.status is ProposalRecordStatus.APPROVAL_REQUIRED and (
        not existing.is_currently_valid_approval(now)
    ):
        reason = "approval expired"
        store.record_audit(
            AuditEventType.RESERVATION_DENIED,
            now,
            proposal_id=proposal.proposal_id,
            snapshot_version=snapshot_version,
            reason=reason,
            conn=conn,
        )
        return _blocked(
            proposal_id=proposal.proposal_id,
            digest=digest,
            reason=reason,
            snapshot_version=snapshot_version,
            idempotent_replay=True,
            authority_result=existing.authority_result,
            expires_at=existing.expires_at,
        )

    store.record_audit(
        AuditEventType.IDEMPOTENT_REPLAY,
        now,
        proposal_id=proposal.proposal_id,
        snapshot_version=existing.snapshot_version,
        reason="duplicate proposal_id; no additional reservation",
        conn=conn,
    )
    reserved = existing.status is ProposalRecordStatus.AUTO_AUTHORIZED
    return OrchestrationResult(
        proposal_id=proposal.proposal_id,
        proposal_hash=digest,
        authority_result=existing.authority_result,
        reserved=reserved,
        blocked=False,
        block_reason=None,
        snapshot_version=existing.snapshot_version,
        idempotent_replay=True,
        decision=None,
        expires_at=existing.expires_at,
    )


def read_reconcile_evaluate_reserve(
    store: SQLiteStore,
    gateway: object,
    proposal: Proposal,
    *,
    now: datetime,
    prices: Mapping[str, Decimal],
    calendar: TradingCalendar = US_TRADING_CALENDAR,
) -> tuple[object, OrchestrationResult]:
    """READ → RECONCILE → EVALUATE → RESERVE → PERSIST. No broker submit."""
    from opaca.broker.gateway import AlpacaGateway
    from opaca.reconciliation.service import reconcile

    if not isinstance(gateway, AlpacaGateway):
        raise PersistenceError("gateway does not implement the read-only AlpacaGateway protocol")
    recon = reconcile(store, gateway, now=now)
    if recon.status is not ReconciliationStatus.RECONCILED or recon.snapshot is None:
        result = OrchestrationResult(
            proposal_id=proposal.proposal_id,
            proposal_hash=proposal_hash(proposal),
            authority_result=None,
            reserved=False,
            blocked=True,
            block_reason=f"reconciliation {recon.status.value}",
            snapshot_version=None if recon.snapshot is None else recon.snapshot.version,
            idempotent_replay=False,
            decision=None,
        )
        return recon, result
    outcome = evaluate_and_reserve(
        store,
        proposal,
        now=now,
        prices=prices,
        expected_snapshot_version=recon.snapshot.version,
        calendar=calendar,
    )
    return recon, outcome
