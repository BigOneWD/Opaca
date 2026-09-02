"""Conservative Wheel exposure and assignment-capital arithmetic."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from opaca.wheel.models import WheelShareLot
from opaca.wheel.store import WheelReservation


@dataclass(frozen=True)
class WheelExposure:
    """Calculated exposure snapshot consumed by later Wheel policy checks."""

    reconciled_cash: Decimal
    active_assignment_commitment: Decimal
    available_cash: Decimal
    held_share_exposure: Mapping[str, Decimal]
    underlying_wheel_exposure: Mapping[str, Decimal]
    aggregate_wheel_exposure: Decimal
    status: str = "KNOWN"
    unknown_underlyings: frozenset[str] = frozenset()


def _decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    return value


def compute_wheel_exposure(
    *,
    reconciled_cash: Decimal,
    share_lots: Sequence[WheelShareLot],
    reservations: Sequence[WheelReservation],
    unattributable_underlyings: Collection[str] = (),
) -> WheelExposure:
    """Compute cash and conservative held-share exposure using Decimal only."""
    cash = _decimal(reconciled_cash, "reconciled_cash")
    commitment_by_underlying: dict[str, Decimal] = {}
    active_commitment = Decimal("0")
    for reservation in reservations:
        amount = _decimal(reservation.amount, "reservation amount")
        if amount < 0:
            raise ValueError("reservation amount must be non-negative")
        if reservation.status != "ACTIVE" or reservation.kind != "CASH_DEPLOYMENT":
            continue
        active_commitment += amount
        commitment_by_underlying[reservation.underlying] = (
            commitment_by_underlying.get(reservation.underlying, Decimal("0")) + amount
        )

    cost_basis_by_underlying: dict[str, Decimal] = {}
    market_value_by_underlying: dict[str, Decimal] = {}
    for lot in share_lots:
        assignment_basis = _decimal(lot.assignment_basis, "assignment basis")
        market_value = _decimal(lot.market_value, "share market value")
        cost_basis = Decimal(lot.shares) * assignment_basis
        cost_basis_by_underlying[lot.underlying] = (
            cost_basis_by_underlying.get(lot.underlying, Decimal("0")) + cost_basis
        )
        market_value_by_underlying[lot.underlying] = (
            market_value_by_underlying.get(lot.underlying, Decimal("0")) + market_value
        )

    held_share_exposure = {
        underlying: max(
            cost_basis_by_underlying.get(underlying, Decimal("0")),
            market_value_by_underlying.get(underlying, Decimal("0")),
        )
        for underlying in cost_basis_by_underlying.keys() | market_value_by_underlying.keys()
    }
    underlying_wheel_exposure = {
        underlying: held_share_exposure.get(underlying, Decimal("0"))
        + commitment_by_underlying.get(underlying, Decimal("0"))
        for underlying in held_share_exposure.keys() | commitment_by_underlying.keys()
    }
    unknown = frozenset(unattributable_underlyings)
    available_cash = cash - active_commitment
    return WheelExposure(
        reconciled_cash=cash,
        active_assignment_commitment=active_commitment,
        available_cash=available_cash,
        held_share_exposure=held_share_exposure,
        underlying_wheel_exposure=underlying_wheel_exposure,
        aggregate_wheel_exposure=sum(held_share_exposure.values(), Decimal("0"))
        + active_commitment,
        status="UNKNOWN" if unknown or available_cash < 0 else "KNOWN",
        unknown_underlyings=unknown,
    )
