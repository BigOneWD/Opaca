"""Teeth: with the reservation mechanism disabled, the P0 probes must FAIL.

These run the same invariant assertions against a deliberately broken variant, so
a green P0-A / P0-B result cannot be vacuous.
"""

from __future__ import annotations

from decimal import Decimal

import opaca.orchestration.context as context_mod
import opaca.policy.engine as engine_mod
from opaca.persistence.store import SQLiteStore

from probe_support import (
    buy_worker,
    reconciled_store,
    reserved_cash,
    reserved_totals,
    run_parallel,
)


def test_teeth_sell_probe_detects_an_oversell(tmp_path, monkeypatch):
    """Neutralise the sell-reservation bound; 60+60 must then oversell 100."""
    monkeypatch.setattr(
        engine_mod, "sell_reservations", lambda orders: ({}, frozenset()), raising=True
    )
    monkeypatch.setattr(
        context_mod, "_sell_unresolved_from_reservations", lambda reservations: [], raising=True
    )
    store, version = reconciled_store(tmp_path, qty="100")
    path = store.path
    store.close()
    from probe_support import sell_worker

    results, errors = run_parallel(
        [sell_worker(path, "t-a", "60", version), sell_worker(path, "t-b", "60", version)]
    )
    assert not errors, errors
    check = SQLiteStore(path)
    total = reserved_totals(check).get("SGOV", Decimal("0"))
    autos = sum(1 for r in results if r is not None and r.is_auto)
    check.close()
    assert autos == 2 and total == Decimal("120"), (autos, total)


def test_teeth_cash_probe_detects_over_deployment(tmp_path, monkeypatch):
    """Neutralise the cash reservation; two 15k buys must then exceed 22k."""
    monkeypatch.setattr(
        context_mod, "_cash_reservation_obligations", lambda reservations, now: (), raising=True
    )
    store, version = reconciled_store(tmp_path, qty=None, cash="100000")
    path = store.path
    store.close()
    results, errors = run_parallel(
        [buy_worker(path, "tb-a", "149", version), buy_worker(path, "tb-b", "149", version)]
    )
    assert not errors, errors
    check = SQLiteStore(path)
    deployed = reserved_cash(check)
    check.close()
    assert deployed > Decimal("22000"), deployed
