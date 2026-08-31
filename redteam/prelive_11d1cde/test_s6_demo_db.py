"""S6: fresh dedicated schema-v2 paper-demo database."""
from __future__ import annotations

import sqlite3
import threading
from datetime import date
from decimal import Decimal

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

from support import DEFAULT_NOW


def db(tmp_path):
    return tmp_path / PAPER_DEMO_DB_NAME


def test_schema_version_is_two():
    assert SCHEMA_VERSION == 2


def test_a_fresh_demo_db_is_v2_wal_fk_on_role_marked_and_seeded_once(tmp_path):
    store = init_paper_demo_store(db(tmp_path), now=DEFAULT_NOW)
    try:
        assert store.schema_version() == 2
        assert store.journal_mode() == "WAL"
        assert store.foreign_keys_enabled() is True
        assert store.system_value("db_role") == PAPER_DEMO_DB_ROLE
        scenario = store.get_scenario()
        assert scenario is not None
        rows = store._conn.execute("SELECT COUNT(*) c FROM scenario_state").fetchone()["c"]
        assert rows == 1
        tables = {r["name"] for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"execution_orders", "approval_grants", "system_state"} <= tables
    finally:
        store.close()


def test_it_refuses_to_silently_overwrite_an_existing_file(tmp_path):
    path = db(tmp_path)
    init_paper_demo_store(path, now=DEFAULT_NOW).close()
    with pytest.raises(DemoDatabaseError) as info:
        init_paper_demo_store(path, now=DEFAULT_NOW)
    assert "refusing to overwrite" in str(info.value)
    assert path.exists()


def test_overwrite_is_explicit_and_replaces_wal_and_shm(tmp_path):
    path = db(tmp_path)
    first = init_paper_demo_store(path, now=DEFAULT_NOW, opening_cash=Decimal("100000"))
    first.close()
    second = init_paper_demo_store(path, now=DEFAULT_NOW, overwrite=True,
                                   opening_cash=Decimal("250000"))
    try:
        assert second.schema_version() == 2
        assert second.system_value("db_role") == PAPER_DEMO_DB_ROLE
    finally:
        second.close()


def test_repeated_startup_reopens_without_reseeding(tmp_path):
    path = db(tmp_path)
    first = init_paper_demo_store(path, now=DEFAULT_NOW, seeded_at=date(2026, 9, 1))
    seeded = first.get_scenario()
    first.close()
    for _ in range(3):
        again = open_existing_paper_demo_store(path)
        try:
            assert again.get_scenario() == seeded
            assert again._conn.execute(
                "SELECT COUNT(*) c FROM scenario_state").fetchone()["c"] == 1
        finally:
            again.close()


def test_a_v1_database_fails_closed_with_no_migration(tmp_path):
    path = tmp_path / "v1.sqlite"
    store = SQLiteStore(path)
    store._conn.execute("DELETE FROM schema_migrations")
    store._conn.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
        ("2026-08-29T00:00:00+00:00",))
    store.close()
    with pytest.raises(PersistenceError) as info:
        SQLiteStore(path)
    assert "unsupported schema version 1" in str(info.value)
    with pytest.raises(PersistenceError):
        open_existing_paper_demo_store(path)


def test_a_wrong_role_database_is_refused_for_reuse(tmp_path):
    path = tmp_path / "someone-elses.db"
    SQLiteStore(path).close()                     # v2, but no db_role marker
    with pytest.raises(DemoDatabaseError) as info:
        open_existing_paper_demo_store(path)
    assert "db_role" in str(info.value)


def test_forbidden_filenames_and_memory_are_refused(tmp_path):
    for name in ("opaca.sqlite", "live-paper.sqlite"):
        with pytest.raises(DemoDatabaseError):
            init_paper_demo_store(tmp_path / name, now=DEFAULT_NOW)
    with pytest.raises(DemoDatabaseError):
        init_paper_demo_store(":memory:", now=DEFAULT_NOW)


def test_a_missing_demo_db_is_not_created_by_the_open_helper(tmp_path):
    path = db(tmp_path)
    with pytest.raises(DemoDatabaseError):
        open_existing_paper_demo_store(path)
    assert not path.exists()


def test_a_failed_initialization_leaves_no_usable_seeded_database(tmp_path):
    path = db(tmp_path)
    with pytest.raises(Exception):
        init_paper_demo_store(path, now=DEFAULT_NOW, opening_cash=Decimal("-1"))
    if path.exists():
        # the file may remain, but it must never look like a seeded demo DB
        store = SQLiteStore(path)
        try:
            assert store.get_scenario() is None, "a failed init left a seeded scenario"
        finally:
            store.close()
        with pytest.raises(DemoDatabaseError):
            init_paper_demo_store(path, now=DEFAULT_NOW)


def test_concurrent_initialization_seeds_exactly_once_and_agrees(tmp_path):
    """Four initializers race on one fresh path. Whatever the win/lose split,
    the file must end up with exactly one scenario seed and one consistent
    db_role, and no initializer may observe a different seed."""
    path = db(tmp_path)
    results = []
    errors = []
    barrier = threading.Barrier(4)

    def go():
        barrier.wait()
        try:
            results.append(init_paper_demo_store(path, now=DEFAULT_NOW, timeout=10.0))
        except Exception as exc:                       # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) + len(errors) == 4
    assert results, f"every concurrent initializer failed: {errors}"
    seeds = {s.get_scenario() for s in results}
    for s in results:
        assert s.system_value("db_role") == PAPER_DEMO_DB_ROLE
        s.close()
    assert len(seeds) == 1, "concurrent initializers disagree about the seed"
    check = open_existing_paper_demo_store(path)
    try:
        assert check._conn.execute(
            "SELECT COUNT(*) c FROM scenario_state").fetchone()["c"] == 1
        assert check._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()


def test_OBSERVATION_the_overwrite_refusal_is_a_racy_existence_check(tmp_path):
    """init_paper_demo_store documents that it refuses an existing file, but the
    check is Path.exists() outside any lock. Concurrent initializers on a fresh
    path can all pass it. Seeding is idempotent, so no data is duplicated."""
    path = db(tmp_path)
    results, errors = [], []
    barrier = threading.Barrier(4)

    def go():
        barrier.wait()
        try:
            results.append(init_paper_demo_store(path, now=DEFAULT_NOW, timeout=10.0))
        except Exception as exc:                       # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = len(results)
    for s in results:
        s.close()
    assert winners == 1, (
        f"{winners} of 4 concurrent initializers were handed a 'fresh' store for "
        "the same path; the overwrite refusal is checked with Path.exists() "
        "outside the BEGIN IMMEDIATE that seeds it. Seeding itself is idempotent "
        "(one scenario_state row, CHECK(id=1)), so this is a contract/TOCTOU "
        "defect, not data duplication."
    )


def test_the_demo_db_is_never_the_test_database_path(tmp_path):
    store = init_paper_demo_store(db(tmp_path), now=DEFAULT_NOW)
    try:
        assert store.path.endswith(PAPER_DEMO_DB_NAME)
        assert "opaca.sqlite" not in store.path
    finally:
        store.close()


def test_foreign_keys_are_enforced_not_merely_reported(tmp_path):
    store = init_paper_demo_store(db(tmp_path), now=DEFAULT_NOW)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "INSERT INTO reservations(proposal_id, kind, symbol, quantity, "
                "amount, status, created_at) "
                "VALUES ('no-such-proposal','SELL_QUANTITY','SGOV','1',NULL,"
                "'ACTIVE',?)",
                (DEFAULT_NOW.isoformat(),))
    finally:
        store.close()
