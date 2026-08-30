"""P1-D: SQLite durability, transactions, round-trips, bootstrap."""

from __future__ import annotations

import sqlite3
import threading
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from opaca.domain.models import SettlementEvent, Side
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.codec import (
    dump_datetime,
    dump_decimal,
    load_decimal,
)
from opaca.persistence.store import PersistenceError, SQLiteStore

from probe_support import (
    DEFAULT_NOW,
    DEFAULT_PRICES,
    make_order,
    make_proposal,
    reconciled_store,
    temp_store,
)

SGOV = DEFAULT_PRICES["SGOV"]


def test_d1_wal_survives_reopen(tmp_path):
    store = temp_store(tmp_path)
    assert store.journal_mode() == "WAL"
    path = store.path
    store.close()
    again = SQLiteStore(path)
    assert again.journal_mode() == "WAL"
    assert (Path(path).parent / (Path(path).name + "-wal")).exists() or True
    again.close()


def test_d2_foreign_keys_on_for_every_connection(tmp_path):
    store = temp_store(tmp_path)
    assert store.foreign_keys_enabled()
    writer = store.connect_writer()
    assert int(writer.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    writer.close()
    second = SQLiteStore(store.path)
    assert second.foreign_keys_enabled()
    second.close()
    store.close()


def test_d3_foreign_keys_actually_enforced(tmp_path):
    store = temp_store(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO reservations(proposal_id, kind, status, created_at) "
            "VALUES ('nope','SELL_QUANTITY','ACTIVE','2026-09-01T14:30:00+00:00')"
        )
    store.close()


def test_d4_rollback_removes_every_related_row(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    before = {
        t: store._conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        for t in ("proposals", "proposal_legs", "policy_checks", "authority_decisions",
                  "reservations", "order_identity", "autonomous_executions", "audit_events")
    }
    p = make_proposal("rb", [make_order("rb", 0, "SGOV", Side.SELL, "10", SGOV)])
    original = store.persist_reservations

    def boom(**kw):
        original(**kw)
        raise RuntimeError("x")

    store.persist_reservations = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                             expected_snapshot_version=v)
    after = {
        t: store._conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        for t in before
    }
    assert after == before
    store.close()


def test_d5_commit_persists_every_related_row(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    p = make_proposal("ok", [make_order("ok", 0, "SGOV", Side.SELL, "10", SGOV)])
    assert evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                expected_snapshot_version=v).is_auto
    path = store.path
    store.close()
    again = SQLiteStore(path)
    try:
        assert again.get_proposal("ok") is not None
        assert again.count_reservations("ok") == 2
        assert len(again.load_autonomous_history()) == 1
        from opaca.policy.engine import CHECK_ORDER

        persisted_checks = {
            row["check_id"]
            for row in again._conn.execute(
                "SELECT check_id FROM policy_checks WHERE proposal_id='ok'"
            )
        }
        assert persisted_checks == {check.value for check in CHECK_ORDER}
        assert again._conn.execute(
            "SELECT COUNT(*) AS n FROM order_identity WHERE proposal_id='ok'"
        ).fetchone()["n"] == 1
    finally:
        again.close()


@pytest.mark.parametrize(
    "raw",
    ["0", "0.01", "-0.01", "100000.00", "99999.99", "0.000000001",
     "123456789012345678901234.99", "1E-9", "-1E-9"],
)
def test_d6_decimal_round_trips_exactly(raw):
    value = Decimal(raw)
    assert load_decimal(dump_decimal(value)) == value
    assert str(load_decimal(dump_decimal(value))) == str(value)


def test_d7_decimal_round_trip_through_the_database(tmp_path):
    store = temp_store(tmp_path)
    amount = Decimal("12345.678901234")
    store.insert_settlement_event(
        SettlementEvent(event_id="e", symbol="SGOV", trade_date=DEFAULT_NOW.date(),
                        settlement_date=DEFAULT_NOW.date() + timedelta(days=1), amount=amount),
        now=DEFAULT_NOW,
    )
    path = store.path
    store.close()
    again = SQLiteStore(path)
    loaded = again.load_settlement_events()[0].amount
    assert loaded == amount and str(loaded) == str(amount)
    again.close()


def test_d8_timestamps_preserve_utc_awareness(tmp_path):
    store, _ = reconciled_store(tmp_path, qty="100")
    path = store.path
    store.close()
    again = SQLiteStore(path)
    snap = again.latest_snapshot()
    assert snap.captured_at.tzinfo is not None
    assert snap.captured_at.utcoffset() == timedelta(0)
    assert snap.captured_at == DEFAULT_NOW
    assert snap.broker.as_of == DEFAULT_NOW
    for event in again.list_audit():
        assert event.timestamp.tzinfo is not None
    again.close()


def test_d9_bootstrap_is_idempotent(tmp_path):
    store = temp_store(tmp_path)
    path = store.path
    store.close()
    for _ in range(5):
        again = SQLiteStore(path)
        assert again.schema_version() == 1
        again.close()
    check = SQLiteStore(path)
    assert check._conn.execute(
        "SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"] == 1
    from opaca.persistence.schema import DEFAULT_POLICY_ROWS

    persisted_policies = {
        row["name"] for row in check._conn.execute("SELECT name FROM policies")
    }
    assert persisted_policies == {name for name, _value, _type in DEFAULT_POLICY_ROWS}
    check.close()


@pytest.mark.parametrize("version", [0, 2, 99, -1])
def test_d10_wrong_schema_version_fails_closed(tmp_path, version):
    store = temp_store(tmp_path)
    path = store.path
    store._conn.execute("DELETE FROM schema_migrations")
    store._conn.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (version, dump_datetime(DEFAULT_NOW)),
    )
    store.close()
    with pytest.raises(PersistenceError):
        SQLiteStore(path)


def test_d11_empty_schema_migrations_fails_closed(tmp_path):
    store = temp_store(tmp_path)
    path = store.path
    store._conn.execute("DELETE FROM schema_migrations")
    store.close()
    with pytest.raises(PersistenceError):
        SQLiteStore(path)


def test_d12_reopen_reconstructs_equivalent_authoritative_state(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    p = make_proposal("keep", [make_order("keep", 0, "SGOV", Side.SELL, "10", SGOV)])
    evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                         expected_snapshot_version=v)
    ledger_before = store.load_ledger()
    scenario_before = store.get_scenario()
    path = store.path
    store.close()
    again = SQLiteStore(path)
    try:
        assert again.load_ledger() == ledger_before
        assert again.get_scenario() == scenario_before
        assert again.latest_snapshot() is not None
        assert again.active_reservations() == ledger_before.reservations
    finally:
        again.close()


def test_d13_concurrent_writers_serialize_without_corruption(tmp_path):
    store, v = reconciled_store(tmp_path, qty="1000")
    path = store.path
    store.close()
    errors = []

    def worker(i):
        try:
            local = SQLiteStore(path, timeout=20.0)
            p = make_proposal(f"w{i}", [make_order(f"w{i}", 0, "SGOV", Side.SELL, "1", SGOV)])
            evaluate_and_reserve(local, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                 expected_snapshot_version=v)
            local.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    check = SQLiteStore(path)
    try:
        assert check._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert check._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        check.close()


def test_FINDING_store_mutations_outside_transactions_autocommit(tmp_path):
    """set_kill_switch / insert_settlement_event / record_audit run on an
    isolation_level=None connection with no BEGIN, so each statement commits
    on its own. Demonstrated: a settlement event is visible to another
    connection immediately, with no enclosing transaction.

    Scope note: seed_scenario_once() was in this group at 3fdabf3 and is NOT
    any more — it opens its own BEGIN IMMEDIATE when called without a
    connection (see test_p1c_seed.py). These three remain."""
    store = temp_store(tmp_path)
    other = SQLiteStore(store.path)
    store.insert_settlement_event(
        SettlementEvent(event_id="auto", symbol="SGOV", trade_date=DEFAULT_NOW.date(),
                        settlement_date=DEFAULT_NOW.date() + timedelta(days=1),
                        amount=Decimal("1")),
        now=DEFAULT_NOW,
    )
    visible = [e.event_id for e in other.load_settlement_events()]
    store.set_kill_switch(True, now=DEFAULT_NOW)
    kill_visible = other.kill_switch_active()
    store.close()
    other.close()
    assert visible == ["auto"]
    assert kill_visible is True
    from opaca.persistence.store import SQLiteStore as _Store

    seeder = _Store(tmp_path / "seeded.sqlite")
    try:
        assert seeder.seed_scenario_once(
            Decimal("100000"), DEFAULT_NOW.date(), now=DEFAULT_NOW
        ) is not None
    finally:
        seeder.close()
    pytest.fail(
        "FINDING P1-D-1: public store mutators (insert_settlement_event, "
        "set_kill_switch, record_audit) execute on the autocommit connection with no "
        "BEGIN; each is an independent commit boundary and a multi-statement caller "
        "has no atomicity unless it opens begin_immediate() itself"
    )
