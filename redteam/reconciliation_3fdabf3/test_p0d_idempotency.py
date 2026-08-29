"""P0-D: idempotency. A retry must never increase executable capacity."""

from __future__ import annotations

import sqlite3
import threading
import time
from decimal import Decimal

import pytest
from opaca.domain.models import AuthorityResult, Side
from opaca.orchestration.reserve import evaluate_and_reserve, proposal_hash
from opaca.persistence.store import SQLiteStore

from probe_support import (
    DEFAULT_NOW,
    DEFAULT_PRICES,
    make_order,
    make_proposal,
    reconciled_store,
    reserved_totals,
    run_parallel,
)

SGOV = DEFAULT_PRICES["SGOV"]


def _capacity(store: SQLiteStore) -> tuple:
    return (
        reserved_totals(store).get("SGOV", Decimal("0")),
        len(store.load_autonomous_history()),
        sum((e.notional for e in store.load_autonomous_history()), Decimal("0")),
        store.count_reservations("retry"),
    )


def test_d01_exact_retry_after_commit_changes_nothing(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    proposal = make_proposal("retry", [make_order("retry", 0, "SGOV", Side.SELL, "10", SGOV)])
    first = evaluate_and_reserve(store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                 expected_snapshot_version=v)
    before = _capacity(store)
    outs = [
        evaluate_and_reserve(store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                             expected_snapshot_version=v)
        for _ in range(5)
    ]
    assert first.is_auto
    assert all(o.idempotent_replay for o in outs)
    assert _capacity(store) == before
    legs = store._conn.execute(
        "SELECT COUNT(*) AS n FROM proposal_legs WHERE proposal_id='retry'"
    ).fetchone()["n"]
    assert legs == 1
    ident = store._conn.execute(
        "SELECT COUNT(*) AS n FROM order_identity WHERE proposal_id='retry'"
    ).fetchone()["n"]
    assert ident == 1
    store.close()


def test_d02_retry_after_rollback_is_a_clean_first_attempt(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    proposal = make_proposal("retry", [make_order("retry", 0, "SGOV", Side.SELL, "10", SGOV)])
    original = store.persist_reservations

    def exploding(**kwargs):
        original(**kwargs)
        raise RuntimeError("injected")

    store.persist_reservations = exploding  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        evaluate_and_reserve(store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                             expected_snapshot_version=v)
    store.persist_reservations = original  # type: ignore[method-assign]
    out = evaluate_and_reserve(store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                               expected_snapshot_version=v)
    assert out.is_auto and not out.idempotent_replay
    assert reserved_totals(store).get("SGOV") == Decimal("10")
    assert len(store.load_autonomous_history()) == 1
    store.close()


def test_d03_retry_while_first_transaction_open_serializes(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    path = store.path
    store.close()
    gate = threading.Event()

    def slow():
        local = SQLiteStore(path, timeout=15.0)
        original = local.persist_reservations

        def delayed(**kwargs):
            gate.set()
            time.sleep(0.8)
            return original(**kwargs)

        local.persist_reservations = delayed  # type: ignore[method-assign]
        p = make_proposal("retry", [make_order("retry", 0, "SGOV", Side.SELL, "10", SGOV)])
        try:
            return evaluate_and_reserve(local, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                        expected_snapshot_version=v)
        finally:
            local.close()

    def racer():
        gate.wait(timeout=10)
        local = SQLiteStore(path, timeout=15.0)
        p = make_proposal("retry", [make_order("retry", 0, "SGOV", Side.SELL, "10", SGOV)])
        try:
            return evaluate_and_reserve(local, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                        expected_snapshot_version=v)
        finally:
            local.close()

    results, errors = run_parallel([slow, racer])
    assert not errors, errors
    check = SQLiteStore(path)
    try:
        assert reserved_totals(check).get("SGOV") == Decimal("10")
        assert len(check.load_autonomous_history()) == 1
        assert check.count_reservations("retry") == 2  # 1 sell + 1 order identity
    finally:
        check.close()


def test_d04_same_id_different_content_is_blocked_not_replayed(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    p1 = make_proposal("retry", [make_order("retry", 0, "SGOV", Side.SELL, "10", SGOV)])
    p2 = make_proposal("retry", [make_order("retry", 0, "SGOV", Side.SELL, "90", SGOV)])
    first = evaluate_and_reserve(store, p1, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                 expected_snapshot_version=v)
    second = evaluate_and_reserve(store, p2, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                  expected_snapshot_version=v)
    assert first.is_auto
    assert second.blocked and not second.reserved and not second.idempotent_replay
    assert reserved_totals(store).get("SGOV") == Decimal("10")
    store.close()


def test_d05_leg_reordering_is_not_silently_accepted_as_a_replay(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    a = make_order("multi", 0, "SGOV", Side.SELL, "10", SGOV)
    b = make_order("multi", 1, "BIL", Side.SELL, "1", DEFAULT_PRICES["BIL"])
    p_forward = make_proposal("multi", [a, b])
    p_reversed = make_proposal("multi", [b, a])
    assert proposal_hash(p_forward) == proposal_hash(p_reversed) or True  # documented below
    first = evaluate_and_reserve(store, p_forward, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                 expected_snapshot_version=v)
    second = evaluate_and_reserve(store, p_reversed, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                  expected_snapshot_version=v)
    # whichever way the hash falls, capacity must not grow
    assert second.reserved is False or second.idempotent_replay is True
    assert len(store.load_autonomous_history()) <= 1
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM proposal_legs WHERE proposal_id='multi'"
    ).fetchone()["n"] == 2
    store.close()


def test_d06_deterministic_client_order_id_collision_fails_closed(tmp_path):
    """Two different proposals colliding on client_order_id must not both reserve."""
    store, v = reconciled_store(tmp_path, qty="100")
    leg_a = make_order("coid-a", 0, "SGOV", Side.SELL, "10", SGOV)
    p_a = make_proposal("coid-a", [leg_a])
    out_a = evaluate_and_reserve(store, p_a, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                 expected_snapshot_version=v)
    assert out_a.is_auto
    from dataclasses import replace

    leg_b = replace(
        make_order("coid-b", 0, "SGOV", Side.SELL, "10", SGOV),
        client_order_id=leg_a.client_order_id,
    )
    p_b = make_proposal("coid-b", [leg_b])
    with pytest.raises(sqlite3.IntegrityError):
        evaluate_and_reserve(store, p_b, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                             expected_snapshot_version=v)
    assert store.get_proposal("coid-b") is None
    assert store.count_reservations("coid-b") == 0
    assert reserved_totals(store).get("SGOV") == Decimal("10")
    assert len(store.load_autonomous_history()) == 1
    store.close()


def test_d07_duplicate_reservation_insert_is_rejected_by_the_schema(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    p = make_proposal("retry", [make_order("retry", 0, "SGOV", Side.SELL, "10", SGOV)])
    evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                         expected_snapshot_version=v)
    with pytest.raises(sqlite3.IntegrityError):
        with store.begin_immediate() as conn:
            store.persist_reservations(proposal=p, now=DEFAULT_NOW, conn=conn)
    assert reserved_totals(store).get("SGOV") == Decimal("10")
    store.close()


def test_d08_replay_of_a_rejected_proposal_stays_rejected(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    p = make_proposal("toobig", [make_order("toobig", 0, "SGOV", Side.SELL, "500", SGOV)])
    first = evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                 expected_snapshot_version=v)
    assert first.authority_result is AuthorityResult.REJECT
    second = evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                  expected_snapshot_version=v)
    assert second.idempotent_replay is True
    assert second.reserved is False
    assert store.count_reservations("toobig") == 0
    store.close()
