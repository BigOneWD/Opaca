"""Red-team support for Phase 3 (paper execution).

Builds the offline world directly from production doubles
(FakeAlpacaGateway / FakePaperExecutionGateway) rather than the builder's
tests/execution_helpers.py, so a defect in the builder's fixtures cannot
mask a defect in the layer under test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from opaca.broker.gateway import FakeAlpacaGateway
from opaca.domain.models import Proposal, Side
from opaca.execution.gateway import FakePaperExecutionGateway
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.store import SQLiteStore
from opaca.persistence.types import ReconciliationStatus, ReservationKind, ReservationStatus
from opaca.reconciliation.service import reconcile

from tests.helpers import DEFAULT_NOW, DEFAULT_PRICES, make_order, make_proposal
from tests.state_helpers import PHASE1_ASSETS, account_payload, position_payload

SGOV = DEFAULT_PRICES["SGOV"]

__all__ = [
    "DEFAULT_NOW",
    "DEFAULT_PRICES",
    "SGOV",
    "World",
    "active_sell_qty",
    "active_cash",
    "buy",
    "make_order",
    "make_proposal",
    "reserve",
    "sell",
    "world",
]


@dataclass
class World:
    store: SQLiteStore
    account: dict[str, object]
    positions: list[dict[str, object]]
    open_orders: list[dict[str, object]] = field(default_factory=list)
    orders: dict[str, dict[str, object]] = field(default_factory=dict)

    def read(self, *, unavailable: bool = False, lookup_unavailable: bool = False):
        gateway = FakeAlpacaGateway(
            account=self.account,
            positions=tuple(self.positions),
            assets=PHASE1_ASSETS,
            open_orders=tuple(self.open_orders),
            orders_by_client_id=self.orders,
            clock={"timestamp": DEFAULT_NOW.isoformat(), "is_open": True},
            unavailable=unavailable,
        )
        gateway.lookup_unavailable = lookup_unavailable
        return gateway

    def mutate(self, **kw) -> FakePaperExecutionGateway:
        params = dict(
            orders=self.orders,
            linked_account=self.account,
            linked_positions=self.positions,
            linked_open_orders=self.open_orders,
            fill_price=SGOV,
        )
        params.update(kw)
        return FakePaperExecutionGateway(**params)

    def close(self) -> None:
        self.store.close()


def world(tmp_path: Path, *, cash: str = "100000", qty: str = "100",
          now: datetime = DEFAULT_NOW) -> World:
    store = SQLiteStore(tmp_path / "opaca.sqlite")
    positions = [dict(position_payload(qty=qty))] if Decimal(qty) > 0 else []
    w = World(store=store, account=dict(account_payload(cash=cash)), positions=positions)
    recon = reconcile(store, w.read(), now=now)
    assert recon.status is ReconciliationStatus.RECONCILED, recon.reasons
    return w


def sell(pid: str, quantity: str, symbol: str = "SGOV") -> Proposal:
    return make_proposal(
        pid, [make_order(pid, 0, symbol, Side.SELL, quantity, DEFAULT_PRICES[symbol])]
    )


def buy(pid: str, quantity: str, symbol: str = "SGOV") -> Proposal:
    return make_proposal(
        pid, [make_order(pid, 0, symbol, Side.BUY, quantity, DEFAULT_PRICES[symbol])]
    )


def reserve(w: World, proposal: Proposal, *, now: datetime = DEFAULT_NOW):
    """Fresh reconcile + reserve, exactly as Phase 2 requires."""
    recon = reconcile(w.store, w.read(), now=now)
    assert recon.status is ReconciliationStatus.RECONCILED, recon.reasons
    outcome = evaluate_and_reserve(
        w.store, proposal, now=now, prices=DEFAULT_PRICES,
        expected_snapshot_version=recon.snapshot.version,
    )
    return recon.snapshot.version, outcome


def active_sell_qty(store: SQLiteStore, symbol: str = "SGOV") -> Decimal:
    return sum(
        (r.quantity for r in store.active_reservations()
         if r.kind is ReservationKind.SELL_QUANTITY and r.symbol == symbol
         and r.quantity is not None and r.status is ReservationStatus.ACTIVE),
        Decimal("0"),
    )


def active_cash(store: SQLiteStore) -> Decimal:
    return sum(
        (r.amount for r in store.active_reservations()
         if r.kind is ReservationKind.CASH_DEPLOYMENT and r.amount is not None
         and r.status is ReservationStatus.ACTIVE),
        Decimal("0"),
    )
