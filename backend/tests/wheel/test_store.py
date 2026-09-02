"""RED-phase contract tests for the isolated Competition Wheel store."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from opaca.wheel.store import (
    WheelAccountBinding,
    WheelAuditEvent,
    WheelOrderRecord,
    WheelReservation,
    WheelStore,
)

ACCOUNT_A = "paper-account-123456789"
ACCOUNT_B = "paper-account-987654321"
NOW = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
EXPECTED_TABLES = {
    "wheel_meta",
    "wheel_reservations",
    "wheel_share_lots",
    "wheel_orders",
    "wheel_approvals",
    "wheel_audit",
}


def _new_store(tmp_path: Path) -> WheelStore:
    return WheelStore(tmp_path / "wheel-test.sqlite3")


def _table_names(store: WheelStore) -> set[str]:
    rows = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _persisted_text(store: WheelStore) -> str:
    fragments: list[str] = []
    for table in _table_names(store):
        if table.startswith("sqlite_"):
            continue
        quoted_table = '"' + table.replace('"', '""') + '"'
        rows = store._conn.execute(f"SELECT * FROM {quoted_table}").fetchall()
        fragments.append(repr([tuple(row) for row in rows]))
    return "\n".join(fragments)


def test_task3_public_store_types_are_exposed() -> None:
    assert all(
        item is not None
        for item in (
            WheelStore,
            WheelAccountBinding,
            WheelReservation,
            WheelOrderRecord,
            WheelAuditEvent,
        )
    )


def test_fresh_wheel_store_uses_wal_and_foreign_keys_without_repo_db_writes(
    tmp_path: Path,
) -> None:
    store = _new_store(tmp_path)
    try:
        journal_mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = store._conn.execute("PRAGMA foreign_keys").fetchone()[0]

        assert str(journal_mode).upper() == "WAL"
        assert int(foreign_keys) == 1
    finally:
        store.close()

    repo_root = Path(__file__).resolve().parents[3]
    assert not (repo_root / "backend" / "opaca-paper-demo.db").exists()
    assert not (repo_root / "backend" / "opaca-wheel-paper.db").exists()
    assert all(path.parent == tmp_path for path in tmp_path.rglob("*"))


def test_wheel_store_exposes_only_the_isolated_v1_tables(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    try:
        user_tables = {name for name in _table_names(store) if not name.startswith("sqlite_")}
        assert user_tables == EXPECTED_TABLES
    finally:
        store.close()


def test_account_binding_persists_full_sha256_but_not_raw_account_id(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    try:
        store.bootstrap_account(ACCOUNT_A, Decimal("99899.58"), NOW)
        persisted = _persisted_text(store)

        assert hashlib.sha256(ACCOUNT_A.encode()).hexdigest() in persisted
        assert ACCOUNT_A not in persisted
    finally:
        store.close()


def test_same_account_bootstrap_is_idempotent_for_risk_capital_base(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    try:
        store.bootstrap_account(ACCOUNT_A, Decimal("99899.58"), NOW)
        assert store.risk_capital_base() == Decimal("99899.58")

        store.bootstrap_account(ACCOUNT_A, Decimal("100500.00"), NOW)

        assert store.risk_capital_base() == Decimal("99899.58")
    finally:
        store.close()


def test_account_mismatch_fails_closed_without_leaking_account_ids(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    try:
        store.bootstrap_account(ACCOUNT_A, Decimal("99899.58"), NOW)

        with pytest.raises(RuntimeError) as binding_error:
            store.assert_account_binding(ACCOUNT_B)
        assert ACCOUNT_A not in str(binding_error.value)
        assert ACCOUNT_B not in str(binding_error.value)

        with pytest.raises(RuntimeError) as bootstrap_error:
            store.bootstrap_account(ACCOUNT_B, Decimal("100500.00"), NOW)
        assert ACCOUNT_A not in str(bootstrap_error.value)
        assert ACCOUNT_B not in str(bootstrap_error.value)
    finally:
        store.close()


def test_account_binding_accepts_the_original_account(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    try:
        store.bootstrap_account(ACCOUNT_A, Decimal("99899.58"), NOW)
        assert store.assert_account_binding(ACCOUNT_A) is None
    finally:
        store.close()


def test_later_cash_like_bootstrap_values_cannot_enlarge_seeded_base(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    try:
        store.bootstrap_account(ACCOUNT_A, Decimal("99899.58"), NOW)
        first = store.risk_capital_base()

        store.active_assignment_reservations()
        store.bootstrap_account(ACCOUNT_A, Decimal("150000.00"), NOW)

        assert first == Decimal("99899.58")
        assert store.risk_capital_base() == first
    finally:
        store.close()


def test_begin_immediate_commits_writes_and_rolls_back_failures(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    trace: list[str] = []
    store._conn.set_trace_callback(trace.append)
    try:
        with store.begin_immediate() as connection:
            assert isinstance(connection, sqlite3.Connection)
            assert connection.in_transaction is True
            connection.execute("CREATE TABLE transaction_probe (value TEXT NOT NULL)")
            connection.execute("INSERT INTO transaction_probe(value) VALUES (?)", ("kept",))
    finally:
        store._conn.set_trace_callback(None)

    assert any(statement.strip().upper() == "BEGIN IMMEDIATE" for statement in trace)
    assert store._conn.execute("SELECT value FROM transaction_probe").fetchone()[0] == "kept"

    with pytest.raises(RuntimeError), store.begin_immediate() as connection:
        connection.execute(
            "INSERT INTO transaction_probe(value) VALUES (?)",
            ("rolled-back",),
        )
        raise RuntimeError("rollback probe")

    values = store._conn.execute("SELECT value FROM transaction_probe ORDER BY rowid").fetchall()
    assert [str(row[0]) for row in values] == ["kept"]
    store.close()


def test_risk_capital_base_round_trips_as_a_canonical_decimal_string(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    try:
        opening_cash = Decimal("99899.58")
        store.bootstrap_account(ACCOUNT_A, opening_cash, NOW)

        assert store.risk_capital_base() == opening_cash
        persisted_values = [
            value
            for table in _table_names(store)
            if not table.startswith("sqlite_")
            for value in (
                item
                for row in store._conn.execute(f'SELECT * FROM "{table}"').fetchall()
                for item in row
            )
        ]
        assert "99899.58" in persisted_values
        assert not any(isinstance(value, float) for value in persisted_values)
    finally:
        store.close()
