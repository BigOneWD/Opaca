"""SQLite schema, WAL, scenario seed-once, and reopen durability."""

from __future__ import annotations

import sqlite3
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from opaca.persistence.schema import SCHEMA_VERSION
from opaca.persistence.store import SQLiteStore
from opaca.treasury.scenario import seed_scenario

from tests.helpers import DEFAULT_NOW


class TestSchemaAndPragmas:
    def test_wal_and_foreign_keys(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "opaca.sqlite")
        assert store.journal_mode() == "WAL"
        assert store.foreign_keys_enabled()
        assert store.schema_version() == SCHEMA_VERSION
        store.close()

    def test_named_policies_are_seeded(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "opaca.sqlite")
        assert store.policy_value("concentration_max_fraction") == "0.70"
        assert store.policy_value("min_trade_notional") == "1.00"
        assert store.policy_value("max_snapshot_age_seconds") == "60"
        assert store.kill_switch_active() is False
        store.close()


class TestScenarioSeedOnce:
    def test_seed_persists_once_against_opening_cash(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "opaca.sqlite")
        first = store.seed_scenario_once(Decimal("99999.99"), date(2026, 9, 1), now=DEFAULT_NOW)
        expected = seed_scenario(Decimal("99999.99"), date(2026, 9, 1))
        assert first.opening_cash == Decimal("99999.99")
        assert first.operating_reserve == expected.operating_reserve
        assert first.obligations[0].amount == expected.obligations[0].amount
        second = store.seed_scenario_once(Decimal("500000"), date(2026, 9, 2), now=DEFAULT_NOW)
        assert second.opening_cash == Decimal("99999.99")
        assert second.operating_reserve == first.operating_reserve
        assert second.obligations[0].amount == first.obligations[0].amount
        store.close()

    def test_later_cash_does_not_reseed(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "opaca.sqlite")
        store.seed_scenario_once(Decimal("100000"), date(2026, 9, 1), now=DEFAULT_NOW)
        again = store.seed_scenario_once(Decimal("90000"), date(2026, 9, 1), now=DEFAULT_NOW)
        assert again.operating_reserve == Decimal("40000.00")
        payroll, suppliers = again.obligations
        assert payroll.amount == Decimal("24000.00")
        assert suppliers.amount == Decimal("14000.00")
        store.close()


class TestReopen:
    def test_mid_seed_failure_leaves_no_partial_seed(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "opaca.sqlite")
        store._conn.execute(
            "INSERT INTO obligations(obligation_id, name, amount, due_date, seeded) "
            "VALUES ('seed-payroll','squatter','1','2026-09-11',0)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.seed_scenario_once(Decimal("99999.99"), date(2026, 9, 1), now=DEFAULT_NOW)
        assert store.get_scenario() is None
        rows = store._conn.execute("SELECT COUNT(*) AS n FROM scenario_state").fetchone()
        assert int(rows["n"]) == 0
        store.close()

    def test_concurrent_direct_seed_creates_exactly_one(self, tmp_path: Path) -> None:
        path = tmp_path / "opaca.sqlite"
        store = SQLiteStore(path)
        store.close()
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def worker(cash: str) -> None:
            local = SQLiteStore(path, timeout=15.0)
            try:
                barrier.wait(timeout=15)
                local.seed_scenario_once(Decimal(cash), date(2026, 9, 1), now=DEFAULT_NOW)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                local.close()

        threads = [
            threading.Thread(target=worker, args=("100000",)),
            threading.Thread(target=worker, args=("150000",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not errors
        check = SQLiteStore(path)
        assert check._conn.execute("SELECT COUNT(*) AS n FROM scenario_state").fetchone()["n"] == 1
        assert check._conn.execute("SELECT COUNT(*) AS n FROM obligations").fetchone()["n"] == 2
        seed = check.get_scenario()
        assert seed is not None
        assert seed.opening_cash in {Decimal("100000"), Decimal("150000")}
        check.close()

    def test_database_reopen_preserves_authoritative_state(self, tmp_path: Path) -> None:
        path = tmp_path / "opaca.sqlite"
        store = SQLiteStore(path)
        store.seed_scenario_once(Decimal("100000"), date(2026, 9, 1), now=DEFAULT_NOW)
        store.close()
        reopened = SQLiteStore(path)
        seed = reopened.get_scenario()
        assert seed is not None
        assert seed.opening_cash == Decimal("100000")
        assert seed.operating_reserve == Decimal("40000.00")
        assert reopened.journal_mode() == "WAL"
        reopened.close()
