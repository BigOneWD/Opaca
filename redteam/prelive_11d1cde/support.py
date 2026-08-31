"""Offline world for the pre-live suite, built from production doubles only."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from opaca.broker.gateway import FakeAlpacaGateway
from opaca.domain.models import Proposal, ProposedOrder, Side
from opaca.execution.gateway import FakePaperExecutionGateway
from opaca.market.binding import bind_buy, bind_sell, bind_single_leg_proposal
from opaca.market.quote import QUOTE_SOURCE_LATEST_TRADE, CanonicalMarketPrice
from opaca.persistence.store import SQLiteStore
from opaca.persistence.types import ReconciliationStatus
from opaca.policy.client_order_id import deterministic_client_order_id
from opaca.reconciliation.service import reconcile

from tests.helpers import DEFAULT_NOW, DEFAULT_PRICES
from tests.state_helpers import PHASE1_ASSETS, account_payload, position_payload

SGOV = DEFAULT_PRICES["SGOV"]

__all__ = [
    "DEFAULT_NOW", "DEFAULT_PRICES", "SGOV", "World", "world", "quote",
    "quotes_for", "leg", "proposal_of", "bind_buy", "bind_sell",
    "bind_single_leg_proposal",
]


def quote(symbol="SGOV", price=None, *, now=DEFAULT_NOW, age_seconds=1,
          source=QUOTE_SOURCE_LATEST_TRADE):
    amount = DEFAULT_PRICES[symbol] if price is None else (
        price if isinstance(price, Decimal) else Decimal(price))
    return CanonicalMarketPrice(
        symbol=symbol,
        price=amount,
        source_timestamp=now - timedelta(seconds=age_seconds),
        fetched_at=now,
        source=source,
    )


def quotes_for(*, now=DEFAULT_NOW, age_seconds=1, sgov=None):
    return {
        "SGOV": quote("SGOV", sgov, now=now, age_seconds=age_seconds),
        "BIL": quote("BIL", None, now=now, age_seconds=age_seconds),
        "SHV": quote("SHV", None, now=now, age_seconds=age_seconds),
    }


def leg(pid, index, symbol, side, qty, price):
    return ProposedOrder(
        proposal_id=pid,
        leg_index=index,
        symbol=symbol,
        side=side,
        quantity=Decimal(qty),
        reference_price=Decimal(price) if not isinstance(price, Decimal) else price,
        client_order_id=deterministic_client_order_id(pid, index),
    )


def proposal_of(pid, legs):
    return Proposal(proposal_id=pid, legs=tuple(legs))


@dataclass
class World:
    store: SQLiteStore
    account: dict
    positions: list
    open_orders: list = field(default_factory=list)
    orders: dict = field(default_factory=dict)

    def read(self, **kw):
        gateway = FakeAlpacaGateway(
            account=self.account,
            positions=tuple(self.positions),
            assets=PHASE1_ASSETS,
            open_orders=tuple(self.open_orders),
            orders_by_client_id=self.orders,
            clock={"timestamp": DEFAULT_NOW.isoformat(), "is_open": True},
            **kw,
        )
        return gateway

    def mutate(self, **kw):
        params = dict(
            orders=self.orders,
            linked_account=self.account,
            linked_positions=self.positions,
            linked_open_orders=self.open_orders,
            fill_price=SGOV,
        )
        params.update(kw)
        return FakePaperExecutionGateway(**params)

    def close(self):
        self.store.close()


def world(tmp_path: Path, *, cash="100000", qty="100", now=DEFAULT_NOW) -> World:
    store = SQLiteStore(tmp_path / "opaca.sqlite")
    positions = [dict(position_payload(qty=qty))] if Decimal(qty) > 0 else []
    w = World(store=store, account=dict(account_payload(cash=cash)), positions=positions)
    recon = reconcile(store, w.read(), now=now)
    assert recon.status is ReconciliationStatus.RECONCILED, recon.reasons
    return w
