"""P0-A: atomic SELL reservation race. Invariant: aggregate reserved <= position."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest
from opaca.domain.models import AuthorityResult, Side
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.store import SQLiteStore, SqliteBusyError

from probe_support import (
    DEFAULT_NOW,
    DEFAULT_PRICES,
    buy_worker,
    make_order,
    make_proposal,
    reconciled_store,
    reserved_totals,
    run_parallel,
    sell_worker,
)


def _autos(results):
    return [r for r in results if r is not None and r.is_auto]


@pytest.mark.parametrize(
    "quantities,position,max_autos",
    [
        (["60", "60"], "100", 1),
        (["50", "50"], "100", 2),
        (["50", "50.000000001"], "100", 1),
        (["30", "30", "30", "30"], "100", 3),
        (["100", "100"], "100", 1),
        (["0.000000001", "100"], "100", 1),
    ],
)
def test_a01_concurrent_sells_never_oversell(tmp_path, quantities, position, max_autos):
    store, version = reconciled_store(tmp_path, qty=position)
    path = store.path
    store.close()
    fns = [sell_worker(path, f"p{i}", q, version) for i, q in enumerate(quantities)]
    results, errors = run_parallel(fns)
    assert not errors, errors
    autos = _autos(results)
    assert len(autos) <= max_autos, [r.block_reason or r.authority_result for r in results]
    check = SQLiteStore(path)
    try:
        total = reserved_totals(check).get("SGOV", Decimal("0"))
        assert total <= Decimal(position), f"oversold: reserved {total} > {position}"
        # every non-reserved outcome must hold nothing at all
        for r in results:
            if r is not None and not r.reserved:
                assert check.count_reservations(r.proposal_id) == 0
    finally:
        check.close()


def test_a02_loser_fails_closed_and_records_no_proposal_capacity(tmp_path):
    store, version = reconciled_store(tmp_path, qty="100")
    path = store.path
    store.close()
    results, errors = run_parallel(
        [sell_worker(path, "race-a", "60", version), sell_worker(path, "race-b", "60", version)]
    )
    assert not errors, errors
    autos = _autos(results)
    assert len(autos) == 1
    loser = [r for r in results if not r.is_auto][0]
    assert loser.reserved is False
    assert loser.authority_result is not AuthorityResult.AUTO
    check = SQLiteStore(path)
    try:
        assert check.count_reservations(loser.proposal_id) == 0
        history = check.load_autonomous_history()
        assert len(history) == 1, "a denied proposal must not consume autonomous capacity"
    finally:
        check.close()


def test_a03_same_proposal_id_twice_concurrently_reserves_once(tmp_path):
    store, version = reconciled_store(tmp_path, qty="100")
    path = store.path
    store.close()
    results, errors = run_parallel(
        [sell_worker(path, "dup", "60", version), sell_worker(path, "dup", "60", version)]
    )
    assert not errors, errors
    check = SQLiteStore(path)
    try:
        assert reserved_totals(check).get("SGOV", Decimal("0")) == Decimal("60")
        assert len(check.load_autonomous_history()) == 1
    finally:
        check.close()


def test_a04_pause_between_evaluate_and_reserve_cannot_oversell(tmp_path):
    """TOCTOU: hold one worker between decide() and persist_reservations()."""
    store, version = reconciled_store(tmp_path, qty="100")
    path = store.path
    store.close()

    gate = threading.Event()

    def slow():
        local = SQLiteStore(path)
        original = local.persist_reservations

        def delayed(**kwargs):
            gate.set()
            time.sleep(1.0)
            return original(**kwargs)

        local.persist_reservations = delayed  # type: ignore[method-assign]
        try:
            proposal = make_proposal(
                "slow", [make_order("slow", 0, "SGOV", Side.SELL, "60", DEFAULT_PRICES["SGOV"])]
            )
            return evaluate_and_reserve(
                local, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                expected_snapshot_version=version,
            )
        finally:
            local.close()

    def fast():
        gate.wait(timeout=10)
        local = SQLiteStore(path, timeout=10.0)
        try:
            proposal = make_proposal(
                "fast", [make_order("fast", 0, "SGOV", Side.SELL, "60", DEFAULT_PRICES["SGOV"])]
            )
            return evaluate_and_reserve(
                local, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                expected_snapshot_version=version,
            )
        finally:
            local.close()

    results, errors = run_parallel([slow, fast])
    assert not errors, errors
    check = SQLiteStore(path)
    try:
        assert reserved_totals(check).get("SGOV", Decimal("0")) <= Decimal("100")
        assert len(_autos(results)) == 1
    finally:
        check.close()


def test_a05_busy_lock_never_becomes_auto(tmp_path):
    store, version = reconciled_store(tmp_path, qty="100")
    path = store.path
    store.close()
    blocker = sqlite3.connect(path, timeout=0, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute(
        "INSERT INTO audit_events(event_type, timestamp, reason, detail, broker_identifiers) "
        "VALUES ('BROKER_STATE_READ','2026-09-01T14:30:00+00:00','probe','','')"
    )
    try:
        local = SQLiteStore(path, timeout=0.05)
        proposal = make_proposal(
            "busy", [make_order("busy", 0, "SGOV", Side.SELL, "10", DEFAULT_PRICES["SGOV"])]
        )
        with pytest.raises((SqliteBusyError, sqlite3.OperationalError)):
            evaluate_and_reserve(
                local, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                expected_snapshot_version=version,
            )
        local.close()
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    check = SQLiteStore(path)
    try:
        assert check.get_proposal("busy") is None
        assert check.count_reservations("busy") == 0
    finally:
        check.close()


def test_a06_rollback_after_reservation_insert_leaves_nothing(tmp_path):
    store, version = reconciled_store(tmp_path, qty="100")
    original = store.persist_reservations

    def exploding(**kwargs):
        original(**kwargs)
        raise RuntimeError("injected after reservation insert")

    store.persist_reservations = exploding  # type: ignore[method-assign]
    proposal = make_proposal(
        "boom", [make_order("boom", 0, "SGOV", Side.SELL, "10", DEFAULT_PRICES["SGOV"])]
    )
    with pytest.raises(RuntimeError):
        evaluate_and_reserve(
            store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
            expected_snapshot_version=version,
        )
    assert store.get_proposal("boom") is None
    assert store.count_reservations("boom") == 0
    assert reserved_totals(store) == {}
    assert store.load_autonomous_history() == ()
    store.close()


PROC_SRC = textwrap.dedent(
    """
    import json, sys
    sys.path.insert(0, sys.argv[1])
    sys.path.insert(0, sys.argv[2])
    from decimal import Decimal
    from opaca.domain.models import Side
    from opaca.orchestration.reserve import evaluate_and_reserve
    from opaca.persistence.store import SQLiteStore
    from tests.helpers import DEFAULT_NOW, DEFAULT_PRICES, make_order, make_proposal

    path, pid, qty, version = sys.argv[3], sys.argv[4], sys.argv[5], int(sys.argv[6])
    store = SQLiteStore(path, timeout=15.0)
    proposal = make_proposal(pid, [make_order(pid, 0, "SGOV", Side.SELL, qty, DEFAULT_PRICES["SGOV"])])
    out = evaluate_and_reserve(
        store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
        expected_snapshot_version=version,
    )
    print(json.dumps({"auto": out.is_auto, "reserved": out.reserved, "blocked": out.blocked}))
    store.close()
    """
)


def test_a07_two_real_processes_cannot_oversell(tmp_path):
    store, version = reconciled_store(tmp_path, qty="100")
    path = store.path
    store.close()
    backend = str(Path(__file__).resolve().parent)
    import opaca

    opaca_root = str(Path(opaca.__file__).resolve().parents[1])
    script = tmp_path / "worker.py"
    script.write_text(PROC_SRC, encoding="utf-8")
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), opaca_root, backend, path, pid, "60", str(version)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for pid in ("proc-a", "proc-b")
    ]
    outs = [p.communicate(timeout=60) for p in procs]
    for (out, err), p in zip(outs, procs, strict=True):
        assert p.returncode == 0, err
    payloads = [__import__("json").loads(out.strip().splitlines()[-1]) for out, _ in outs]
    assert sum(1 for p in payloads if p["auto"]) == 1, payloads
    check = SQLiteStore(path)
    try:
        assert reserved_totals(check).get("SGOV", Decimal("0")) == Decimal("60")
    finally:
        check.close()
