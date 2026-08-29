"""P1-H: failure injection at every transaction boundary. All-or-nothing."""

from __future__ import annotations

import sqlite3

import pytest
from opaca.domain.models import Side
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.store import SQLiteStore
from opaca.persistence.types import AuditEventType

from probe_support import (
    DEFAULT_NOW,
    DEFAULT_PRICES,
    make_order,
    make_proposal,
    reconciled_store,
)

SGOV = DEFAULT_PRICES["SGOV"]
TABLES = (
    "proposals", "proposal_legs", "policy_checks", "authority_decisions",
    "reservations", "order_identity", "autonomous_executions", "approvals",
)


def snapshot_counts(store):
    return {
        t: store._conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in TABLES
    }


def run_with_injection(tmp_path, patch):
    store, v = reconciled_store(tmp_path, qty="100")
    before = snapshot_counts(store)
    undo = patch(store)
    p = make_proposal("inj", [make_order("inj", 0, "SGOV", Side.SELL, "10", SGOV)])
    raised = None
    try:
        evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                             expected_snapshot_version=v)
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    if undo is not None:
        undo()
    after = snapshot_counts(store)
    path = store.path
    try:
        store.close()
    except Exception:
        pass
    reopened = SQLiteStore(path)
    durable = snapshot_counts(reopened)
    orphans = reopened._conn.execute(
        "SELECT COUNT(*) AS n FROM reservations r "
        "LEFT JOIN proposals p ON p.proposal_id = r.proposal_id WHERE p.proposal_id IS NULL"
    ).fetchone()["n"]
    reopened.close()
    return raised, before, after, durable, orphans


def _patch_method(store, name, mode="raise"):
    original = getattr(store, name)

    def failing(*a, **kw):
        if mode == "before":
            raise RuntimeError(f"injected before {name}")
        result = original(*a, **kw)
        raise RuntimeError(f"injected after {name}")

    setattr(store, name, failing)
    return lambda: setattr(store, name, original)


@pytest.mark.parametrize(
    "name,mode",
    [
        ("begin_immediate", "before"),
        ("latest_snapshot", "before"),
        ("persist_proposal_decision", "before"),
        ("persist_proposal_decision", "after"),
        ("persist_reservations", "before"),
        ("persist_reservations", "after"),
        ("record_audit", "before"),
        ("policy_value", "before"),
    ],
)
def test_h1_injection_leaves_all_or_nothing(tmp_path, name, mode):
    raised, before, after, durable, orphans = run_with_injection(
        tmp_path, lambda s: _patch_method(s, name, mode)
    )
    assert raised is not None, f"{name}/{mode} did not fail"
    assert after == before, f"{name}/{mode} left rows: {after} vs {before}"
    assert durable == before, f"{name}/{mode} durably left rows: {durable} vs {before}"
    assert orphans == 0


class CommitFailingConn:
    """Transparent proxy that fails the COMMIT statement."""

    def __init__(self, conn, exc):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_exc", exc)
        object.__setattr__(self, "_armed", True)

    def execute(self, sql, *a, **kw):
        if (
            object.__getattribute__(self, "_armed")
            and isinstance(sql, str)
            and sql.strip().upper().startswith("COMMIT")
        ):
            raise object.__getattribute__(self, "_exc")
        return object.__getattribute__(self, "_conn").execute(sql, *a, **kw)

    def disarm(self):
        object.__setattr__(self, "_armed", False)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)


def test_h2_failure_before_commit_leaves_nothing(tmp_path):
    def patch(store):
        real = store._conn
        proxy = CommitFailingConn(real, RuntimeError("injected just before COMMIT"))
        store._conn = proxy
        def undo():
            proxy.disarm()
            store._conn = real
            if real.in_transaction:
                real.execute("ROLLBACK")
        return undo

    raised, before, after, durable, orphans = run_with_injection(tmp_path, patch)
    assert raised is not None
    assert durable == before, durable
    assert orphans == 0


def test_h3_commit_failure_leaves_nothing_durable(tmp_path):
    def patch(store):
        real = store._conn
        proxy = CommitFailingConn(real, sqlite3.OperationalError("disk I/O error on commit"))
        store._conn = proxy
        def undo():
            proxy.disarm()
            store._conn = real
            if real.in_transaction:
                real.execute("ROLLBACK")
        return undo

    raised, before, after, durable, orphans = run_with_injection(tmp_path, patch)
    assert isinstance(raised, sqlite3.OperationalError)
    assert durable == before, durable
    assert orphans == 0


def test_h4_no_orphan_reservation_after_any_injection(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    for i, name in enumerate(
        ["persist_reservations", "record_audit", "persist_proposal_decision"]
    ):
        undo = _patch_method(store, name, "after")
        p = make_proposal(f"o{i}", [make_order(f"o{i}", 0, "SGOV", Side.SELL, "1", SGOV)])
        with pytest.raises(RuntimeError):
            evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                 expected_snapshot_version=v)
        undo()
        assert store.count_reservations(f"o{i}") == 0
        assert store.get_proposal(f"o{i}") is None
    assert store.active_reservations() == ()
    store.close()


def test_FINDING_commit_failure_leaves_the_connection_in_a_transaction(tmp_path):
    """begin_immediate() issues ROLLBACK only on an exception raised by the body. If
    COMMIT itself fails, no ROLLBACK is issued and the connection stays inside the
    transaction; the uncommitted rows remain live and visible on that connection."""
    store, v = reconciled_store(tmp_path, qty="100")
    real = store._conn
    proxy = CommitFailingConn(real, sqlite3.OperationalError("disk I/O error on commit"))
    store._conn = proxy
    p = make_proposal("c1", [make_order("c1", 0, "SGOV", Side.SELL, "10", SGOV)])
    with pytest.raises(sqlite3.OperationalError):
        evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                             expected_snapshot_version=v)
    proxy.disarm()
    still_open = real.in_transaction
    leaked = store.get_proposal("c1")
    second_error = None
    try:
        p2 = make_proposal("c2", [make_order("c2", 0, "SGOV", Side.SELL, "1", SGOV)])
        evaluate_and_reserve(store, p2, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                             expected_snapshot_version=v)
    except BaseException as exc:  # noqa: BLE001
        second_error = exc
    store._conn = real
    if real.in_transaction:
        real.execute("ROLLBACK")
    store.close()
    assert still_open is True, "probe assumption"
    pytest.fail(
        "FINDING P1-H-1: after a COMMIT failure the store connection is still inside "
        "the transaction (begin_immediate's else branch has no ROLLBACK); the "
        f"uncommitted proposal is still readable on that connection ({leaked is not None}) "
        f"and the next orchestration call raises "
        f"{type(second_error).__name__ if second_error else 'nothing'}"
    )
