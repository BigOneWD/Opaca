"""Fresh schema-v2 paper-demo DB. Never a test DB. Never auto-deleted."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from opaca.persistence.demo import (
    PAPER_DEMO_DB_NAME,
    PAPER_DEMO_DB_ROLE,
    DemoDatabaseError,
    init_paper_demo_store,
    open_existing_paper_demo_store,
)
from opaca.persistence.schema import SCHEMA_VERSION
from opaca.persistence.store import PersistenceError, SQLiteStore

from tests.helpers import DEFAULT_NOW
from tests.state_helpers import temp_store


class TestFreshDemoDb:
    def test_fresh_db_created(self, tmp_path: Path) -> None:
        path = tmp_path / PAPER_DEMO_DB_NAME
        store = init_paper_demo_store(path, now=DEFAULT_NOW)
        assert store.schema_version() == SCHEMA_VERSION
        assert SCHEMA_VERSION == 2
        assert store.journal_mode() == "WAL"
        assert store.foreign_keys_enabled() is True
        assert store.system_value("db_role") == PAPER_DEMO_DB_ROLE
        seed = store.get_scenario()
        assert seed is not None
        assert seed.opening_cash == Decimal("100000")
        store.close()

    def test_existing_db_not_overwritten(self, tmp_path: Path) -> None:
        path = tmp_path / PAPER_DEMO_DB_NAME
        path.write_text("keep-me", encoding="utf-8")
        with pytest.raises(DemoDatabaseError, match="refusing to overwrite"):
            init_paper_demo_store(path, now=DEFAULT_NOW)
        assert path.read_text(encoding="utf-8") == "keep-me"

    def test_v1_db_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.sqlite"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
            ("2026-01-01T00:00:00+00:00",),
        )
        conn.commit()
        conn.close()
        with pytest.raises(PersistenceError, match="unsupported schema version 1"):
            SQLiteStore(path)
        demo_path = tmp_path / PAPER_DEMO_DB_NAME
        demo_path.write_bytes(path.read_bytes())
        with pytest.raises(DemoDatabaseError, match="refusing to overwrite"):
            init_paper_demo_store(demo_path, now=DEFAULT_NOW)

    def test_seed_once(self, tmp_path: Path) -> None:
        path = tmp_path / PAPER_DEMO_DB_NAME
        store = init_paper_demo_store(path, now=DEFAULT_NOW, opening_cash=Decimal("100000"))
        first = store.get_scenario()
        assert first is not None
        second = store.seed_scenario_once(Decimal("500000"), first.seeded_at, now=DEFAULT_NOW)
        assert second.opening_cash == Decimal("100000")
        store.close()
        reopened = open_existing_paper_demo_store(path)
        seed = reopened.get_scenario()
        assert seed is not None
        assert seed.opening_cash == Decimal("100000")
        reopened.close()

    def test_test_db_filename_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(DemoDatabaseError, match="test/live-smoke filename"):
            init_paper_demo_store(tmp_path / "opaca.sqlite", now=DEFAULT_NOW)

    def test_demo_db_is_not_test_db(self, tmp_path: Path) -> None:
        test_store = temp_store(tmp_path)
        assert Path(test_store.path).name == "opaca.sqlite"
        assert test_store.system_value("db_role") is None
        test_store.close()
        demo = init_paper_demo_store(tmp_path / PAPER_DEMO_DB_NAME, now=DEFAULT_NOW)
        assert Path(demo.path).name == PAPER_DEMO_DB_NAME
        assert demo.system_value("db_role") == PAPER_DEMO_DB_ROLE
        demo.close()

    def test_overwrite_rebuilds_schema_v2(self, tmp_path: Path) -> None:
        path = tmp_path / PAPER_DEMO_DB_NAME
        path.write_text("junk", encoding="utf-8")
        store = init_paper_demo_store(path, now=DEFAULT_NOW, overwrite=True)
        assert store.schema_version() == 2
        assert store.system_value("db_role") == PAPER_DEMO_DB_ROLE
        store.close()
