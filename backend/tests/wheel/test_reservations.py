"""RED-phase contracts for Wheel assignment reservations."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from opaca.wheel.store import WheelStore

ACCOUNT_ID = "paper-account-123456789"
NOW = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)


def new_store(tmp_path: Path) -> WheelStore:
    store = WheelStore(tmp_path / "wheel-reservations.sqlite3")
    store.bootstrap_account(ACCOUNT_ID, Decimal("100000"), NOW)
    return store


def test_one_logical_csp_has_one_active_assignment_reservation(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    try:
        store.reserve_assignment(
            reservation_id="xyz-csp",
            underlying="XYZ",
            amount=Decimal("25000"),
            now=NOW,
        )
        store.reserve_assignment(
            reservation_id="xyz-csp",
            underlying="XYZ",
            amount=Decimal("25000"),
            now=NOW,
        )

        active = store.active_assignment_reservations()
        assert len(active) == 1
        assert active[0].amount == Decimal("25000")
    finally:
        store.close()


def test_timeout_or_unknown_does_not_release_assignment_capacity(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    try:
        store.reserve_assignment(
            reservation_id="unknown-csp",
            underlying="XYZ",
            amount=Decimal("25000"),
            now=NOW,
        )

        released = store.release_assignment_if_proven_no_exposure(
            "unknown-csp",
            proven=False,
            now=NOW,
        )

        assert released is False
        assert [item.reservation_id for item in store.active_assignment_reservations()] == [
            "unknown-csp"
        ]
    finally:
        store.close()


def test_proven_zero_exposure_can_release_specific_reservation(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    try:
        store.reserve_assignment(
            reservation_id="terminal-csp",
            underlying="XYZ",
            amount=Decimal("25000"),
            now=NOW,
        )

        released = store.release_assignment_if_proven_no_exposure(
            "terminal-csp",
            proven=True,
            now=NOW,
        )

        assert released is True
        assert store.active_assignment_reservations() == []
        status = store._conn.execute(
            "SELECT status FROM wheel_reservations WHERE reservation_id = ?",
            ("terminal-csp",),
        ).fetchone()[0]
        assert status == "RELEASED"
    finally:
        store.close()


def test_assignment_conversion_is_atomic_and_conservative(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    trace: list[str] = []
    store._conn.set_trace_callback(trace.append)
    try:
        store.reserve_assignment(
            reservation_id="assigned-csp",
            underlying="XYZ",
            amount=Decimal("25000"),
            now=NOW,
        )
        store._conn.execute(
            """
            CREATE TRIGGER fail_share_lot_insert
            BEFORE INSERT ON wheel_share_lots
            BEGIN
                SELECT RAISE(ABORT, 'share lot conversion failed');
            END
            """
        )

        with pytest.raises(sqlite3.IntegrityError):
            store.convert_assignment_to_share_lot(
                reservation_id="assigned-csp",
                lot_id="xyz-lot-1",
                underlying="XYZ",
                shares=100,
                assignment_basis=Decimal("250"),
                market_value=Decimal("25000"),
                now=NOW,
            )

        assert [item.reservation_id for item in store.active_assignment_reservations()] == [
            "assigned-csp"
        ]
        assert store._conn.execute("SELECT COUNT(*) FROM wheel_share_lots").fetchone()[0] == 0

        store._conn.execute("DROP TRIGGER fail_share_lot_insert")
        store.convert_assignment_to_share_lot(
            reservation_id="assigned-csp",
            lot_id="xyz-lot-1",
            underlying="XYZ",
            shares=100,
            assignment_basis=Decimal("250"),
            market_value=Decimal("25000"),
            now=NOW,
        )

        assert store.active_assignment_reservations() == []
        lot = store._conn.execute(
            "SELECT underlying, shares, cost_basis FROM wheel_share_lots WHERE lot_id = ?",
            ("xyz-lot-1",),
        ).fetchone()
        assert tuple(lot) == ("XYZ", "100", "250.00")
        assert any(statement.strip().upper() == "BEGIN IMMEDIATE" for statement in trace)
    finally:
        store._conn.set_trace_callback(None)
        store.close()
