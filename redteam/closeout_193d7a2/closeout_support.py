"""Offline world + real-gateway harness for the 193d7a2 closeout suite.

Merged into one module (rather than reusing ``prelive_11d1cde/support.py``)
so that pytest's prepend import mode cannot bind ``support`` to whichever
suite happened to be collected first.
"""
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


# ----------------------------------------------------------------------------
# Closeout harness: real AlpacaPaperExecutionGateway + boundary-clock control
# ----------------------------------------------------------------------------


import time
from datetime import timedelta
from decimal import Decimal

import opaca.execution.service as service
from opaca.broker.paper_execution import AlpacaPaperExecutionGateway
from opaca.execution.gateway import PaperOrderRequest
from opaca.market.binding import bind_buy, bind_sell, bind_single_leg_proposal
from opaca.reconciliation.service import reconcile
from opaca.orchestration.reserve import evaluate_and_reserve


PAPER_URL = "https://paper-api.alpaca.markets"


class StubTradingClient:
    """Shape-compatible stand-in for alpaca TradingClient.

    It carries exactly the attributes the production paper guard inspects
    (`_base_url`, `_paper`) and the two mutators the real gateway binds. It is
    only ever used to construct the REAL AlpacaPaperExecutionGateway class, so
    every production guard on that path runs for real. Nothing leaves process.
    """

    def __init__(self, base_url: str = PAPER_URL, paper: bool = True) -> None:
        self._base_url = base_url
        self._paper = paper
        self.submit_calls = 0
        self.cancel_calls = 0
        self.orders: dict[str, dict[str, object]] = {}
        self.seen: list[object] = []

    def submit_order(self, order_data: object = None) -> dict[str, object]:
        self.submit_calls += 1
        self.seen.append(order_data)
        cid = str(getattr(order_data, "client_order_id", "unknown"))
        qty = str(getattr(order_data, "qty", "0"))
        limit = getattr(order_data, "limit_price", None)
        payload: dict[str, object] = {
            "id": f"broker-{self.submit_calls}",
            "client_order_id": cid,
            "symbol": str(getattr(order_data, "symbol", "SGOV")),
            "side": "buy",
            "status": "new",
            "qty": qty,
            "filled_qty": "0",
            "filled_avg_price": None,
            "order_type": "limit",
            "type": "limit",
        }
        if limit is not None:
            payload["limit_price"] = str(limit)
        self.orders[cid] = payload
        return payload

    def cancel_order_by_id(self, order_id: object) -> None:
        self.cancel_calls += 1


def real_paper_gateway(base_url: str = PAPER_URL) -> tuple[AlpacaPaperExecutionGateway, StubTradingClient]:
    """A genuine AlpacaPaperExecutionGateway instance. No network, no credentials."""
    client = StubTradingClient(base_url=base_url)
    return AlpacaPaperExecutionGateway(client), client


def counting_fake(w, **kw):
    """FakePaperExecutionGateway wired to the world (submit_calls is on it)."""
    return w.mutate(**kw)


def buy_setup(w, pid="retest-1", qty="3", now=DEFAULT_NOW, age_seconds=1, sgov=None):
    quotes = quotes_for(now=now, age_seconds=age_seconds, sgov=sgov)
    bound = bind_buy(quotes["SGOV"], Decimal(qty))
    proposal, prices, bindings = bind_single_leg_proposal(pid, bound, quotes)
    recon = reconcile(w.store, w.read(), now=now)
    out = evaluate_and_reserve(
        w.store, proposal, now=now, prices=prices,
        expected_snapshot_version=recon.snapshot.version, price_bindings=bindings)
    return proposal, prices, dict(bindings), out


class BoundaryClock:
    """Patches service._utc_now. Records every read. Optional side effect."""

    def __init__(self, value=None, offset_real_from=None, on_read=None):
        self.value = value
        self.offset_real_from = offset_real_from
        self.on_read = on_read
        self.reads: list[object] = []
        self._t0 = time.monotonic()

    def __call__(self):
        if self.on_read is not None:
            self.on_read()
        if self.offset_real_from is not None:
            elapsed = time.monotonic() - self._t0
            out = self.offset_real_from + timedelta(seconds=elapsed)
        else:
            out = self.value
        self.reads.append(out)
        return out

    def install(self, monkeypatch):
        monkeypatch.setattr(service, "_utc_now", self)
        return self


def order_states(store, proposal_id):
    return [r.state.value for r in store.list_execution_orders(proposal_id=proposal_id)]
