"""RED-phase contracts for Wheel order lifecycle and fail-closed release."""

from __future__ import annotations

from pathlib import Path

import pytest
from opaca.wheel.lifecycle import (
    WheelOrderState,
    record_wheel_order_state,
)
from tests.wheel.test_atomic_reservation import NOW, authorize, new_store


@pytest.mark.parametrize(
    "state",
    [WheelOrderState.UNKNOWN, WheelOrderState.CANCEL_PENDING, WheelOrderState.SUBMITTED],
)
def test_unknown_timeout_and_cancel_pending_retain_reservation(
    tmp_path: Path,
    state: WheelOrderState,
) -> None:
    with new_store(tmp_path / f"retain-{state.value}.sqlite3") as store:
        order = authorize(store, f"retain-{state.value}")

        record_wheel_order_state(
            store,
            order.client_order_id,
            state,
            filled_contracts=0,
            exact_occ_position_present=False,
            unresolved_client_order=True,
            now=NOW,
        )

        assert len(store.active_assignment_reservations()) == 1


@pytest.mark.parametrize(
    "state",
    [
        WheelOrderState.REJECTED,
        WheelOrderState.CANCELLED,
        WheelOrderState.EXPIRED,
        WheelOrderState.NOT_SUBMITTED,
    ],
)
def test_terminal_zero_exposure_with_exact_absence_releases_reservation(
    tmp_path: Path,
    state: WheelOrderState,
) -> None:
    with new_store(tmp_path / f"release-{state.value}.sqlite3") as store:
        order = authorize(store, f"release-{state.value}")

        released = record_wheel_order_state(
            store,
            order.client_order_id,
            state,
            filled_contracts=0,
            exact_occ_position_present=False,
            unresolved_client_order=False,
            now=NOW,
        )

        assert released is True
        assert store.active_assignment_reservations() == []


def test_terminal_state_does_not_release_without_positive_absence_proof(tmp_path: Path) -> None:
    with new_store(tmp_path / "release-proof.sqlite3") as store:
        order = authorize(store, "release-proof")

        released = record_wheel_order_state(
            store,
            order.client_order_id,
            WheelOrderState.REJECTED,
            filled_contracts=0,
            exact_occ_position_present=True,
            unresolved_client_order=False,
            now=NOW,
        )

        assert released is False
        assert len(store.active_assignment_reservations()) == 1


def test_filled_order_keeps_assignment_reservation(tmp_path: Path) -> None:
    with new_store(tmp_path / "filled.sqlite3") as store:
        order = authorize(store, "filled")

        record_wheel_order_state(
            store,
            order.client_order_id,
            WheelOrderState.FILLED,
            filled_contracts=1,
            exact_occ_position_present=True,
            unresolved_client_order=False,
            now=NOW,
        )

        assert len(store.active_assignment_reservations()) == 1


def test_duplicate_logical_reservation_is_idempotent(tmp_path: Path) -> None:
    with new_store(tmp_path / "duplicate.sqlite3") as store:
        first = authorize(store, "same-logical-order")
        second = authorize(store, "same-logical-order")

        assert second == first
        assert len(store.active_assignment_reservations()) == 1
        assert store._conn.execute("SELECT COUNT(*) FROM wheel_orders").fetchone()[0] == 1
