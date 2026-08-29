"""P1-C: exactly one authoritative scenario seed."""

from __future__ import annotations

import sqlite3
import threading
from decimal import Decimal

import pytest
from opaca.persistence.store import SQLiteStore
from opaca.persistence.types import ReconciliationStatus
from opaca.reconciliation.service import reconcile

from probe_support import DEFAULT_NOW, paper_gateway, position_payload, temp_store

OPENING = "99999.99"


def _seeded(tmp_path):
    store = temp_store(tmp_path)
    r = reconcile(store, paper_gateway(cash=OPENING,
                                       positions=(position_payload(qty="100"),)),
                  now=DEFAULT_NOW)
    assert r.status is ReconciliationStatus.RECONCILED
    return store, store.get_scenario()


def test_c1_seed_values_derive_from_opening_cash(tmp_path):
    store, seed = _seeded(tmp_path)
    assert seed.opening_cash == Decimal(OPENING)
    assert seed.operating_reserve == Decimal("39999.99")
    assert seed.obligations_total == Decimal("37999.98")
    store.close()


@pytest.mark.parametrize("later_cash", ["80000", "150000", "500000", "0.01", "99999.98"])
def test_c2_later_cash_never_reseeds(tmp_path, later_cash):
    store, seed = _seeded(tmp_path)
    for _ in range(3):
        reconcile(store, paper_gateway(cash=later_cash,
                                       positions=(position_payload(qty="100"),)),
                  now=DEFAULT_NOW)
    again = store.get_scenario()
    assert again.opening_cash == seed.opening_cash
    assert again.operating_reserve == seed.operating_reserve
    assert again.obligations == seed.obligations
    rows = store._conn.execute("SELECT COUNT(*) AS n FROM scenario_state").fetchone()["n"]
    assert rows == 1
    store.close()


def test_c3_reopen_and_restart_do_not_reseed(tmp_path):
    store, seed = _seeded(tmp_path)
    path = store.path
    store.close()
    for cash in ("150000", "500000"):
        reopened = SQLiteStore(path)
        reconcile(reopened, paper_gateway(cash=cash,
                                          positions=(position_payload(qty="100"),)),
                  now=DEFAULT_NOW)
        assert reopened.get_scenario().opening_cash == seed.opening_cash
        reopened.close()
    check = SQLiteStore(path)
    assert check._conn.execute("SELECT COUNT(*) AS n FROM scenario_state").fetchone()["n"] == 1
    assert check._conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE event_type='SCENARIO_SEEDED'"
    ).fetchone()["n"] == 1
    check.close()


def test_c4_repeated_direct_initialization_is_idempotent(tmp_path):
    store, seed = _seeded(tmp_path)
    for cash in ("1", "500000", "99999.99"):
        again = store.seed_scenario_once(Decimal(cash), DEFAULT_NOW.date(), now=DEFAULT_NOW)
        assert again.opening_cash == seed.opening_cash
    assert store._conn.execute("SELECT COUNT(*) AS n FROM obligations").fetchone()["n"] == 2
    store.close()


def test_c5_concurrent_initialization_produces_one_seed(tmp_path):
    store = temp_store(tmp_path)
    path = store.path
    store.close()
    barrier = threading.Barrier(4)
    errors = []

    def worker(cash):
        try:
            local = SQLiteStore(path, timeout=15.0)
            barrier.wait(timeout=15)
            reconcile(local, paper_gateway(cash=cash,
                                           positions=(position_payload(qty="100"),)),
                      now=DEFAULT_NOW)
            local.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(c,))
               for c in ("99999.99", "150000", "500000", "80000")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    check = SQLiteStore(path)
    try:
        assert check._conn.execute(
            "SELECT COUNT(*) AS n FROM scenario_state").fetchone()["n"] == 1
        assert check._conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events WHERE event_type='SCENARIO_SEEDED'"
        ).fetchone()["n"] == 1
        assert check._conn.execute(
            "SELECT COUNT(*) AS n FROM obligations").fetchone()["n"] == 2
    finally:
        check.close()


def test_c6_crash_during_first_seed_transaction_leaves_no_seed(tmp_path):
    store = temp_store(tmp_path)
    original = store.seed_scenario_once

    def exploding(*a, **kw):
        original(*a, **kw)
        raise RuntimeError("crash during seed")

    store.seed_scenario_once = exploding  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        reconcile(store, paper_gateway(cash=OPENING,
                                       positions=(position_payload(qty="100"),)),
                  now=DEFAULT_NOW)
    assert store.get_scenario() is None
    assert store.latest_snapshot() is None
    store.seed_scenario_once = original  # type: ignore[method-assign]
    r = reconcile(store, paper_gateway(cash="150000",
                                       positions=(position_payload(qty="100"),)),
                  now=DEFAULT_NOW)
    assert r.status is ReconciliationStatus.RECONCILED
    assert store.get_scenario().opening_cash == Decimal("150000")
    store.close()


def test_c7_non_reconciled_first_pass_does_not_seed(tmp_path):
    from tests.state_helpers import order_payload

    store = temp_store(tmp_path)
    r = reconcile(
        store,
        paper_gateway(positions=(position_payload(qty="100"),),
                      open_orders=(order_payload("ghost", status="new"),)),
        now=DEFAULT_NOW,
    )
    assert r.status is not ReconciliationStatus.RECONCILED
    assert store.get_scenario() is None
    store.close()


def test_FINDING_direct_seed_without_transaction_is_not_atomic(tmp_path):
    """seed_scenario_once(conn=None) runs on the autocommit connection: the
    scenario row commits before its obligations, so a mid-seed failure leaves a
    seeded scenario with no (or partial) obligations."""
    store = temp_store(tmp_path)
    store._conn.execute(
        "INSERT INTO obligations(obligation_id, name, amount, due_date, seeded) "
        "VALUES ('seed-payroll','squatter','1','2026-09-11',0)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.seed_scenario_once(Decimal(OPENING), DEFAULT_NOW.date(), now=DEFAULT_NOW)
    scenario_rows = store._conn.execute(
        "SELECT COUNT(*) AS n FROM scenario_state").fetchone()["n"]
    store.close()
    assert scenario_rows == 1, "probe assumption"
    pytest.fail(
        "FINDING P1-C-1: seed_scenario_once() with no explicit connection is not "
        "transactional; scenario_state was committed while its obligations failed, "
        "leaving an authoritative-looking seed with the wrong obligations"
    )
