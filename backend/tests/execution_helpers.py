"""Offline fixtures for the paper execution lifecycle."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from opaca.broker.gateway import FakeAlpacaGateway
from opaca.domain.models import Proposal, Side
from opaca.execution.gateway import FakePaperExecutionGateway
from opaca.market.binding import BoundExecutionPrice, bind_buy, bind_sell
from opaca.orchestration.reserve import OrchestrationResult, evaluate_and_reserve
from opaca.persistence.store import SQLiteStore
from opaca.persistence.types import ReconciliationStatus
from opaca.reconciliation.service import reconcile

from tests.helpers import DEFAULT_NOW, DEFAULT_PRICES, make_order, make_proposal
from tests.market_helpers import universe_quotes
from tests.state_helpers import PHASE1_ASSETS, account_payload, position_payload, temp_store


@dataclass
class PaperWorld:
    store: SQLiteStore
    account: dict[str, object]
    positions: list[dict[str, object]]
    open_orders: list[dict[str, object]] = field(default_factory=list)
    orders: dict[str, dict[str, object]] = field(default_factory=dict)

    def read(self, *, unavailable: bool = False) -> FakeAlpacaGateway:
        return FakeAlpacaGateway(
            account=self.account,
            positions=tuple(self.positions),
            assets=PHASE1_ASSETS,
            open_orders=tuple(self.open_orders),
            orders_by_client_id=self.orders,
            clock={
                "timestamp": DEFAULT_NOW.isoformat(),
                "is_open": True,
                "next_open": "2026-09-02T13:30:00+00:00",
                "next_close": "2026-09-01T20:00:00+00:00",
            },
            unavailable=unavailable,
        )

    def mutate(
        self,
        *,
        timeout_before_accept: bool = False,
        timeout_after_accept: bool = False,
        reject_reason: str | None = None,
        reject_on_call: int | None = None,
        fill_on_submit: bool = True,
        partial_fill_qty: Decimal | None = None,
    ) -> FakePaperExecutionGateway:
        return FakePaperExecutionGateway(
            orders=self.orders,
            linked_account=self.account,
            linked_positions=self.positions,
            linked_open_orders=self.open_orders,
            fill_price=DEFAULT_PRICES["SGOV"],
            timeout_before_accept=timeout_before_accept,
            timeout_after_accept=timeout_after_accept,
            reject_reason=reject_reason,
            reject_on_call=reject_on_call,
            fill_on_submit=fill_on_submit,
            partial_fill_qty=partial_fill_qty,
        )


def make_world(
    tmp_path: Path,
    *,
    cash: str = "100000",
    qty: str = "100",
    now: datetime = DEFAULT_NOW,
) -> PaperWorld:
    store = temp_store(tmp_path)
    positions = [position_payload(qty=qty)] if Decimal(qty) > 0 else []
    world = PaperWorld(
        store=store,
        account=account_payload(cash=cash),
        positions=positions,
    )
    recon = reconcile(store, world.read(), now=now)
    assert recon.status is ReconciliationStatus.RECONCILED
    return world


def bindings_for_proposal(
    proposal: Proposal,
    *,
    now: datetime = DEFAULT_NOW,
    age_seconds: int = 1,
) -> dict[str, BoundExecutionPrice]:
    quotes = universe_quotes(now=now, age_seconds=age_seconds)
    bindings: dict[str, BoundExecutionPrice] = {}
    for item in proposal.legs:
        quote = quotes[item.symbol]
        if item.side is Side.BUY:
            bound = bind_buy(quote, item.quantity, tolerance=Decimal("0"))
        else:
            bound = bind_sell(quote, item.quantity)
        bindings[item.symbol] = bound
    return bindings


@contextmanager
def freeze_submit_clock(when: datetime) -> Iterator[None]:
    with patch("opaca.execution.service._utc_now", return_value=when):
        yield


def reserve_proposal(
    world: PaperWorld,
    proposal: Proposal,
    *,
    now: datetime = DEFAULT_NOW,
) -> tuple[int, OrchestrationResult]:
    recon = reconcile(world.store, world.read(), now=now)
    assert recon.status is ReconciliationStatus.RECONCILED
    assert recon.snapshot is not None
    outcome = evaluate_and_reserve(
        world.store,
        proposal,
        now=now,
        prices=DEFAULT_PRICES,
        expected_snapshot_version=recon.snapshot.version,
    )
    return recon.snapshot.version, outcome


def buy_one(pid: str = "buy-1") -> Proposal:
    return make_proposal(pid, [make_order(pid, 0, "SGOV", Side.BUY, "1", DEFAULT_PRICES["SGOV"])])


def sell_qty(pid: str, quantity: str) -> Proposal:
    return make_proposal(
        pid, [make_order(pid, 0, "SGOV", Side.SELL, quantity, DEFAULT_PRICES["SGOV"])]
    )


def sell_legs(pid: str, quantities: tuple[str, ...]) -> Proposal:
    return make_proposal(
        pid,
        [
            make_order(pid, index, "SGOV", Side.SELL, quantity, DEFAULT_PRICES["SGOV"])
            for index, quantity in enumerate(quantities)
        ],
    )


def active_capacity(store: SQLiteStore, proposal_id: str) -> int:
    return sum(
        1
        for item in store.active_reservations()
        if item.proposal_id == proposal_id
        and item.kind.value in {"SELL_QUANTITY", "CASH_DEPLOYMENT"}
    )
