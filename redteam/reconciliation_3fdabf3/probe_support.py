"""Red-team probe support. Independent of the builder's test helpers where possible."""

from __future__ import annotations

import threading
from decimal import Decimal
from typing import Any

from opaca.domain.models import Side
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.store import SQLiteStore
from opaca.persistence.types import ReconciliationStatus
from opaca.reconciliation.service import reconcile
from tests.helpers import DEFAULT_NOW, DEFAULT_PRICES, make_order, make_proposal
from tests.state_helpers import paper_gateway, position_payload, temp_store

__all__ = [
    "DEFAULT_NOW",
    "DEFAULT_PRICES",
    "Side",
    "SQLiteStore",
    "evaluate_and_reserve",
    "make_order",
    "make_proposal",
    "paper_gateway",
    "position_payload",
    "reconcile",
    "temp_store",
    "reconciled_store",
    "run_parallel",
    "reserved_totals",
]


def reconciled_store(tmp_path, *, qty: str | None = "100", cash: str = "100000") -> tuple[SQLiteStore, int]:
    store = temp_store(tmp_path)
    positions = () if qty is None else (position_payload(qty=qty),)
    recon = reconcile(store, paper_gateway(cash=cash, positions=positions), now=DEFAULT_NOW)
    assert recon.status is ReconciliationStatus.RECONCILED, recon.reasons
    assert recon.snapshot is not None
    return store, recon.snapshot.version


def run_parallel(fns: list[Any], timeout: float = 30.0) -> tuple[list[Any], list[BaseException]]:
    """Run callables truly concurrently, released by a shared barrier."""
    n = len(fns)
    results: list[Any] = [None] * n
    errors: list[BaseException] = []
    barrier = threading.Barrier(n)

    def wrap(i: int) -> None:
        try:
            barrier.wait(timeout=timeout)
            results[i] = fns[i]()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=wrap, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
    for t in threads:
        assert not t.is_alive(), "probe thread did not terminate"
    return results, errors


def sell_worker(path: str, proposal_id: str, qty: str, version: int | None, symbol: str = "SGOV"):
    def run():
        local = SQLiteStore(path)
        try:
            proposal = make_proposal(
                proposal_id,
                [make_order(proposal_id, 0, symbol, Side.SELL, qty, DEFAULT_PRICES[symbol])],
            )
            return evaluate_and_reserve(
                local,
                proposal,
                now=DEFAULT_NOW,
                prices=DEFAULT_PRICES,
                expected_snapshot_version=version,
            )
        finally:
            local.close()

    return run


def buy_worker(path: str, proposal_id: str, qty: str, version: int | None, symbol: str = "SGOV"):
    def run():
        local = SQLiteStore(path)
        try:
            proposal = make_proposal(
                proposal_id,
                [make_order(proposal_id, 0, symbol, Side.BUY, qty, DEFAULT_PRICES[symbol])],
            )
            return evaluate_and_reserve(
                local,
                proposal,
                now=DEFAULT_NOW,
                prices=DEFAULT_PRICES,
                expected_snapshot_version=version,
            )
        finally:
            local.close()

    return run


def reserved_totals(store: SQLiteStore) -> dict[str, Decimal]:
    """Aggregate ACTIVE sell-quantity reservations by symbol."""
    out: dict[str, Decimal] = {}
    for r in store.active_reservations():
        if r.symbol is not None and r.quantity is not None and r.kind.value == "SELL_QUANTITY":
            out[r.symbol] = out.get(r.symbol, Decimal("0")) + r.quantity
    return out


def reserved_cash(store: SQLiteStore) -> Decimal:
    total = Decimal("0")
    for r in store.active_reservations():
        if r.kind.value == "CASH_DEPLOYMENT" and r.amount is not None:
            total += r.amount
    return total
