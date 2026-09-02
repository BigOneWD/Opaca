"""Atomic, broker-independent Wheel order reservation lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from opaca.domain.models import AuthorityDecision, AuthorityResult
from opaca.persistence.codec import dump_datetime, dump_decimal, load_decimal
from opaca.wheel.authority import WheelAuthorityContext, decide_wheel_authority
from opaca.wheel.order_id import wheel_client_order_id
from opaca.wheel.policy import (
    WheelGuardEngine,
    WheelPolicyContext,
    WheelProposal,
    assignment_capital,
)
from opaca.wheel.store import WheelPersistenceError, WheelReservation, WheelStore


class WheelLifecycleError(RuntimeError):
    """Fail-closed lifecycle error."""


class WheelStaleSnapshotError(WheelLifecycleError):
    """The final reservation check did not use the expected snapshot."""


class WheelOrderState(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    NOT_SUBMITTED = "NOT_SUBMITTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class AuthorizedWheelOrder:
    """The one logical Wheel order and its active assignment reservation."""

    client_order_id: str
    reservation_id: str
    assignment_capital: Decimal
    state: WheelOrderState


def _require_utc(value: datetime, name: str) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(None):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return dump_datetime(value)


def _active_reservations(store: WheelStore) -> tuple[WheelReservation, ...]:
    return tuple(store.active_assignment_reservations())


def authorize_and_reserve(
    store: WheelStore,
    *,
    account_id: str,
    expected_snapshot_version: str,
    proposal: WheelProposal,
    policy_context: WheelPolicyContext,
    authority: AuthorityDecision,
    wheel_decision_run_id: str,
    attempt_number: int,
    now: datetime,
) -> AuthorizedWheelOrder:
    """Atomically recheck policy and reserve cash for one logical order."""
    occurred_at = _require_utc(now, "now")
    if not isinstance(expected_snapshot_version, str) or not expected_snapshot_version.strip():
        raise ValueError("expected_snapshot_version must be non-empty")
    authority_result = authority.result
    if authority_result is not AuthorityResult.AUTO:
        raise WheelLifecycleError("only AUTO or freshly approved authority may reserve")
    if not authority.policy_decision.passed:
        raise WheelLifecycleError("hard policy did not pass")

    client_order_id = wheel_client_order_id(
        wheel_decision_run_id=wheel_decision_run_id,
        attempt_number=attempt_number,
        occ_symbol=proposal.contract.occ_symbol,
        action=proposal.action,
        contracts=proposal.contracts,
        limit_premium=proposal.sell_limit_premium,
    )
    capital = assignment_capital(proposal)
    reservation_id = f"{client_order_id}-reservation"

    with store.begin_immediate() as connection:
        store.assert_account_binding(account_id)
        current_snapshot_version = store.snapshot_version()
        if current_snapshot_version != expected_snapshot_version:
            raise WheelStaleSnapshotError(
                f"snapshot {current_snapshot_version!r} does not match "
                f"expected {expected_snapshot_version!r}"
            )

        existing = connection.execute(
            "SELECT client_order_id, occ_symbol, status, reservation_id, assignment_capital "
            "FROM wheel_orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["occ_symbol"]) != proposal.contract.occ_symbol:
                raise WheelLifecycleError("logical order identity is already bound")
            if str(existing["status"]) == WheelOrderState.UNKNOWN.value:
                raise WheelLifecycleError("UNKNOWN logical order suppresses duplicate retry")
            stored_reservation = existing["reservation_id"]
            stored_capital = existing["assignment_capital"]
            if stored_reservation is None or stored_capital is None:
                raise WheelPersistenceError("incomplete Wheel order record")
            return AuthorizedWheelOrder(
                client_order_id=client_order_id,
                reservation_id=str(stored_reservation),
                assignment_capital=load_decimal(str(stored_capital)),
                state=WheelOrderState(str(existing["status"])),
            )

        stored_capital = store.risk_capital_base()
        if policy_context.risk_capital_base != stored_capital:
            raise WheelLifecycleError("risk capital does not match the bound Wheel account")
        live_context = replace(
            policy_context,
            reservations=_active_reservations(store),
        )
        live_policy = WheelGuardEngine().evaluate(live_context, proposal)
        if not live_policy.passed:
            details = "; ".join(result.detail for result in live_policy.violations)
            raise WheelLifecycleError(f"final Wheel policy recheck failed: {details}")
        active_reservations = live_context.reservations
        active_total = sum(
            (reservation.amount for reservation in active_reservations),
            Decimal("0"),
        )
        post_name = (
            live_context.held_share_exposure.get(proposal.contract.underlying, Decimal("0"))
            + sum(
                (
                    reservation.amount
                    for reservation in active_reservations
                    if reservation.underlying == proposal.contract.underlying
                ),
                Decimal("0"),
            )
            + capital
        )
        post_aggregate = sum(
            live_context.held_share_exposure.values(), Decimal("0")
        ) + active_total + capital
        live_authority = decide_wheel_authority(
            WheelAuthorityContext(
                risk_capital_base=live_context.risk_capital_base,
                proposed_assignment_capital=capital,
                post_trade_underlying_exposure=post_name,
                post_trade_aggregate_exposure=post_aggregate,
                policy_decision=live_policy,
            )
        )
        if live_authority.result is not AuthorityResult.AUTO:
            raise WheelLifecycleError("final Wheel delegated authority recheck failed")

        connection.execute(
            "INSERT INTO wheel_reservations "
            "(reservation_id, underlying, amount, status, kind) VALUES (?, ?, ?, ?, ?)",
            (
                reservation_id,
                proposal.contract.underlying,
                dump_decimal(capital),
                "ACTIVE",
                "CASH_DEPLOYMENT",
            ),
        )
        connection.execute(
            "INSERT INTO wheel_orders "
            "(client_order_id, occ_symbol, status, reservation_id, assignment_capital, "
            "snapshot_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                client_order_id,
                proposal.contract.occ_symbol,
                WheelOrderState.AUTHORIZED.value,
                reservation_id,
                dump_decimal(capital),
                expected_snapshot_version,
                occurred_at,
            ),
        )
    return AuthorizedWheelOrder(
        client_order_id=client_order_id,
        reservation_id=reservation_id,
        assignment_capital=capital,
        state=WheelOrderState.AUTHORIZED,
    )


_RELEASEABLE_TERMINAL_STATES = frozenset(
    {
        WheelOrderState.REJECTED,
        WheelOrderState.CANCELLED,
        WheelOrderState.EXPIRED,
        WheelOrderState.NOT_SUBMITTED,
    }
)


def record_wheel_order_state(
    store: WheelStore,
    client_order_id: str,
    state: WheelOrderState,
    *,
    filled_contracts: int | None,
    exact_occ_position_present: bool | None,
    unresolved_client_order: bool | None,
    now: datetime,
) -> bool:
    """Persist a lifecycle state and release only on complete absence proof."""
    occurred_at = _require_utc(now, "now")
    if not isinstance(state, WheelOrderState):
        raise TypeError("state must be a WheelOrderState")
    if filled_contracts is not None and (
        isinstance(filled_contracts, bool) or filled_contracts < 0
    ):
        raise ValueError("filled_contracts must be a non-negative integer")
    with store.begin_immediate() as connection:
        existing = connection.execute(
            "SELECT reservation_id FROM wheel_orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if existing is None:
            raise WheelLifecycleError("logical Wheel order is missing")
        connection.execute(
            "UPDATE wheel_orders SET status = ?, created_at = ? WHERE client_order_id = ?",
            (state.value, occurred_at, client_order_id),
        )
        proven_no_exposure = (
            state in _RELEASEABLE_TERMINAL_STATES
            and filled_contracts == 0
            and exact_occ_position_present is False
            and unresolved_client_order is False
        )
        if not proven_no_exposure or existing["reservation_id"] is None:
            return False
        cursor = connection.execute(
            "UPDATE wheel_reservations SET status = ? "
            "WHERE reservation_id = ? AND kind = ? AND status = ?",
            ("RELEASED", str(existing["reservation_id"]), "CASH_DEPLOYMENT", "ACTIVE"),
        )
        return cursor.rowcount == 1
