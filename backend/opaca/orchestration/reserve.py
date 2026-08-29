"""Atomic evaluate → reserve → persist. No broker submission."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from opaca.calendar.us_trading_calendar import US_TRADING_CALENDAR, TradingCalendar
from opaca.domain.models import AuthorityDecision, AuthorityResult, Proposal
from opaca.orchestration.context import build_policy_context
from opaca.persistence.store import PersistenceError, SQLiteStore, StaleSnapshotError
from opaca.persistence.types import (
    AuditEventType,
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

    @property
    def is_auto(self) -> bool:
        return self.authority_result is AuthorityResult.AUTO and self.reserved and not self.blocked


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
    """
    digest = proposal_hash(proposal)
    try:
        with store.begin_immediate() as conn:
            existing = store.get_proposal(proposal.proposal_id, conn=conn)
            if existing is not None:
                if existing.proposal_hash != digest:
                    store.record_audit(
                        AuditEventType.RESERVATION_DENIED,
                        now,
                        proposal_id=proposal.proposal_id,
                        snapshot_version=existing.snapshot_version,
                        reason="proposal_id reused with a different payload",
                        conn=conn,
                    )
                    return OrchestrationResult(
                        proposal_id=proposal.proposal_id,
                        proposal_hash=digest,
                        authority_result=None,
                        reserved=False,
                        blocked=True,
                        block_reason="proposal_id reused with a different payload",
                        snapshot_version=existing.snapshot_version,
                        idempotent_replay=False,
                        decision=None,
                    )
                store.record_audit(
                    AuditEventType.IDEMPOTENT_REPLAY,
                    now,
                    proposal_id=proposal.proposal_id,
                    snapshot_version=existing.snapshot_version,
                    reason="duplicate proposal_id; no additional reservation",
                    conn=conn,
                )
                return OrchestrationResult(
                    proposal_id=proposal.proposal_id,
                    proposal_hash=digest,
                    authority_result=existing.authority_result,
                    reserved=existing.status is ProposalRecordStatus.AUTO_AUTHORIZED,
                    blocked=False,
                    block_reason=None,
                    snapshot_version=existing.snapshot_version,
                    idempotent_replay=True,
                    decision=None,
                )

            snapshot = store.latest_snapshot(conn=conn)
            if snapshot is None:
                store.record_audit(
                    AuditEventType.RESERVATION_DENIED,
                    now,
                    proposal_id=proposal.proposal_id,
                    reason="no reconciled snapshot",
                    conn=conn,
                )
                return OrchestrationResult(
                    proposal_id=proposal.proposal_id,
                    proposal_hash=digest,
                    authority_result=None,
                    reserved=False,
                    blocked=True,
                    block_reason="no reconciled snapshot",
                    snapshot_version=None,
                    idempotent_replay=False,
                    decision=None,
                )
            if snapshot.reconciliation_status is not ReconciliationStatus.RECONCILED:
                reason = (
                    f"latest snapshot is {snapshot.reconciliation_status.value}; "
                    "uncertainty must never create a trade"
                )
                store.record_audit(
                    AuditEventType.RESERVATION_DENIED,
                    now,
                    proposal_id=proposal.proposal_id,
                    snapshot_version=snapshot.version,
                    reason=reason,
                    conn=conn,
                )
                return OrchestrationResult(
                    proposal_id=proposal.proposal_id,
                    proposal_hash=digest,
                    authority_result=None,
                    reserved=False,
                    blocked=True,
                    block_reason=reason,
                    snapshot_version=snapshot.version,
                    idempotent_replay=False,
                    decision=None,
                )
            if (
                expected_snapshot_version is not None
                and expected_snapshot_version != snapshot.version
            ):
                store.record_audit(
                    AuditEventType.STALE_SNAPSHOT,
                    now,
                    proposal_id=proposal.proposal_id,
                    snapshot_version=snapshot.version,
                    reason=(
                        f"stale snapshot {expected_snapshot_version} vs current {snapshot.version}"
                    ),
                    conn=conn,
                )
                return OrchestrationResult(
                    proposal_id=proposal.proposal_id,
                    proposal_hash=digest,
                    authority_result=None,
                    reserved=False,
                    blocked=True,
                    block_reason="stale snapshot",
                    snapshot_version=snapshot.version,
                    idempotent_replay=False,
                    decision=None,
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
            store.persist_proposal_decision(
                proposal=proposal,
                proposal_hash=digest,
                status=status,
                decision=decision,
                snapshot=snapshot,
                now=now,
                expires_at=expires_at
                if decision.result is AuthorityResult.APPROVAL_REQUIRED
                else None,
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
            )
    except StaleSnapshotError as exc:
        return OrchestrationResult(
            proposal_id=proposal.proposal_id,
            proposal_hash=digest,
            authority_result=None,
            reserved=False,
            blocked=True,
            block_reason=str(exc),
            snapshot_version=None,
            idempotent_replay=False,
            decision=None,
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
