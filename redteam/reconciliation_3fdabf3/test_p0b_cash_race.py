"""P0-B: concurrent BUY proposals against the same deployable cash + rolling authority."""

from __future__ import annotations

from decimal import Decimal

import pytest
from opaca.domain.models import AuthorityResult, Side
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.store import SQLiteStore

from probe_support import (
    DEFAULT_NOW,
    DEFAULT_PRICES,
    buy_worker,
    make_order,
    make_proposal,
    reconciled_store,
    reserved_cash,
    run_parallel,
    sell_worker,
)

SGOV = DEFAULT_PRICES["SGOV"]


def _investable(store: SQLiteStore) -> Decimal:
    scenario = store.get_scenario()
    assert scenario is not None
    snap = store.latest_snapshot()
    assert snap is not None
    return snap.broker.cash - scenario.operating_reserve - scenario.obligations_total


def test_b00_scenario_shape_is_the_one_under_attack(tmp_path):
    store, _ = reconciled_store(tmp_path, qty=None, cash="100000")
    assert _investable(store) == Decimal("22000"), _investable(store)
    store.close()


def test_b01_two_concurrent_buys_cannot_exceed_deployable_cash(tmp_path):
    store, version = reconciled_store(tmp_path, qty=None, cash="100000")
    limit = _investable(store)
    path = store.path
    store.close()
    results, errors = run_parallel(
        [buy_worker(path, "buy-a", "149", version), buy_worker(path, "buy-b", "149", version)]
    )
    assert not errors, errors
    autos = [r for r in results if r is not None and r.is_auto]
    check = SQLiteStore(path)
    try:
        deployed = reserved_cash(check)
        assert deployed <= limit, f"aggregate deployment {deployed} exceeds deployable {limit}"
        assert len(autos) == 1, [r.authority_result for r in results]
    finally:
        check.close()


@pytest.mark.parametrize("n", [3, 4])
def test_b02_many_concurrent_buys_bounded_by_deployable_cash(tmp_path, n):
    store, version = reconciled_store(tmp_path, qty=None, cash="100000")
    limit = _investable(store)
    path = store.path
    store.close()
    results, errors = run_parallel(
        [buy_worker(path, f"m{i}", "99", version) for i in range(n)]
    )
    assert not errors, errors
    check = SQLiteStore(path)
    try:
        deployed = reserved_cash(check)
        assert deployed <= limit, f"aggregate deployment {deployed} exceeds deployable {limit}"
    finally:
        check.close()


def test_b03_sequential_buys_also_bounded(tmp_path):
    """Non-concurrent control: the second buy must see the first reservation."""
    store, version = reconciled_store(tmp_path, qty=None, cash="100000")
    limit = _investable(store)
    outcomes = []
    for i in range(3):
        pid = f"seq{i}"
        proposal = make_proposal(
            pid, [make_order(pid, 0, "SGOV", Side.BUY, "99", SGOV)]
        )
        outcomes.append(
            evaluate_and_reserve(
                store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                expected_snapshot_version=version,
            )
        )
    deployed = reserved_cash(store)
    assert deployed <= limit, f"{deployed} > {limit}"
    assert any(o.authority_result is not AuthorityResult.AUTO for o in outcomes)
    store.close()


def test_b04_rolling_24h_authority_bounded_under_concurrency(tmp_path):
    """rolling_24h_autonomous_notional_max = 50,000; three ~20k sells must not all pass."""
    store, version = reconciled_store(tmp_path, qty="1000", cash="100000")
    path = store.path
    store.close()
    results, errors = run_parallel(
        [sell_worker(path, f"roll{i}", "200", version) for i in range(3)]
    )
    assert not errors, errors
    check = SQLiteStore(path)
    try:
        total = sum((e.notional for e in check.load_autonomous_history()), Decimal("0"))
        assert total <= Decimal("50000"), f"rolling 24h consumption {total} exceeds 50000"
    finally:
        check.close()


def test_b05_rolling_order_count_bounded_under_concurrency(tmp_path):
    """runaway_hourly_order_count_max = 6 must hold across concurrent workers."""
    store, version = reconciled_store(tmp_path, qty="1000", cash="100000")
    path = store.path
    store.close()
    results, errors = run_parallel(
        [sell_worker(path, f"cnt{i}", "1", version) for i in range(9)]
    )
    assert not errors, errors
    check = SQLiteStore(path)
    try:
        history = check.load_autonomous_history()
        assert len(history) <= 6, f"{len(history)} autonomous executions in one hour"
    finally:
        check.close()


def test_b06_cash_reservation_is_actually_recorded(tmp_path):
    store, version = reconciled_store(tmp_path, qty=None, cash="100000")
    proposal = make_proposal("one", [make_order("one", 0, "SGOV", Side.BUY, "99", SGOV)])
    out = evaluate_and_reserve(
        store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
        expected_snapshot_version=version,
    )
    assert out.is_auto
    assert reserved_cash(store) == proposal.total_buy_notional
    store.close()
