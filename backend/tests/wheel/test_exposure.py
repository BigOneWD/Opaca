"""RED-phase contracts for Wheel exposure arithmetic."""

from __future__ import annotations

from decimal import Decimal
from typing import cast

import pytest
from opaca.wheel.exposure import compute_wheel_exposure
from opaca.wheel.models import WheelShareLot
from opaca.wheel.store import WheelReservation


def reservation(
    reservation_id: str,
    underlying: str,
    amount: str,
    status: str = "ACTIVE",
) -> WheelReservation:
    return WheelReservation(
        reservation_id=reservation_id,
        underlying=underlying,
        amount=Decimal(amount),
        status=status,
    )


def test_exposure_uses_decimal_reservations_and_conservative_share_value() -> None:
    lots = [
        WheelShareLot(
            underlying="XYZ",
            shares=100,
            assignment_basis=Decimal("250"),
            market_value=Decimal("24000"),
        ),
        WheelShareLot(
            underlying="ABC",
            shares=50,
            assignment_basis=Decimal("100"),
            market_value=Decimal("6000"),
        ),
    ]
    reservations = [
        reservation("xyz-csp", "XYZ", "12000"),
        reservation("abc-csp", "ABC", "3000"),
        reservation("released", "XYZ", "9000", status="RELEASED"),
    ]

    exposure = compute_wheel_exposure(
        reconciled_cash=Decimal("100000"),
        share_lots=lots,
        reservations=reservations,
    )

    assert exposure.active_assignment_commitment == Decimal("15000")
    assert exposure.available_cash == Decimal("85000")
    assert exposure.held_share_exposure["XYZ"] == Decimal("25000")
    assert exposure.underlying_wheel_exposure["XYZ"] == Decimal("37000")
    assert exposure.aggregate_wheel_exposure == Decimal("46000")
    assert isinstance(exposure.aggregate_wheel_exposure, Decimal)


def test_unattributable_broker_shares_are_unknown_not_zero() -> None:
    exposure = compute_wheel_exposure(
        reconciled_cash=Decimal("100000"),
        share_lots=(),
        reservations=(),
        unattributable_underlyings={"XYZ"},
    )

    assert exposure.status == "UNKNOWN"
    assert "XYZ" in exposure.unknown_underlyings


def test_assigned_shares_keep_same_name_exposure_after_reservation_release() -> None:
    lots = (
        WheelShareLot(
            underlying="XYZ",
            shares=100,
            assignment_basis=Decimal("250"),
            market_value=Decimal("26000"),
        ),
    )
    exposure = compute_wheel_exposure(
        reconciled_cash=Decimal("100000"),
        share_lots=lots,
        reservations=(),
    )

    assert exposure.held_share_exposure["XYZ"] == Decimal("26000")
    assert exposure.underlying_wheel_exposure["XYZ"] >= Decimal("25000")


def test_float_financial_input_fails_closed() -> None:
    with pytest.raises(ValueError):
        compute_wheel_exposure(
            reconciled_cash=cast(Decimal, 100000.0),
            share_lots=(),
            reservations=(),
        )
