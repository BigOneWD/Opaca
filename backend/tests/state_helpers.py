"""Offline fixtures for the reconciliation/persistence layer."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import cast

from opaca.broker.gateway import ASSET_UNIVERSE, FakeAlpacaGateway
from opaca.persistence.store import SQLiteStore

from tests.helpers import (
    DEFAULT_NOW,
    DEFAULT_PRICES,
    load_evidence,
    phase1_account_fields,
    phase1_asset_states,
)

PHASE1_ACCOUNT = phase1_account_fields()
PHASE1_ASSETS = {
    symbol: {
        "symbol": symbol,
        "status": state.status.value,
        "tradable": state.tradable,
        "fractionable": state.fractionable,
    }
    for symbol, state in phase1_asset_states().items()
}


def account_payload(
    cash: str | Decimal = "100000",
    buying_power: str | Decimal | None = None,
    non_marginable: str | Decimal | None = None,
    multiplier: str | Decimal = "4",
) -> dict[str, object]:
    cash_d = Decimal(cash) if not isinstance(cash, Decimal) else cash
    bp = Decimal("400000") if buying_power is None else Decimal(str(buying_power))
    nm = cash_d if non_marginable is None else Decimal(str(non_marginable))
    payload = dict(PHASE1_ACCOUNT)
    payload["cash"] = format(cash_d, "f")
    payload["buying_power"] = format(bp, "f")
    payload["non_marginable_buying_power"] = format(nm, "f")
    payload["multiplier"] = str(multiplier)
    return payload


def position_payload(
    symbol: str = "SGOV",
    qty: str = "100",
    qty_available: str | None = None,
    market_value: str | None = None,
    price: Decimal = DEFAULT_PRICES["SGOV"],
) -> dict[str, object]:
    quantity = Decimal(qty)
    available = Decimal(qty_available) if qty_available is not None else quantity
    value = Decimal(market_value) if market_value is not None else quantity * price
    return {
        "symbol": symbol,
        "side": "long",
        "qty": format(quantity, "f"),
        "qty_available": format(available, "f"),
        "market_value": format(value, "f"),
    }


def order_payload(
    client_order_id: str,
    symbol: str = "SGOV",
    side: str = "sell",
    status: str = "new",
    qty: str | None = "10",
    filled_qty: str | None = "0",
    broker_id: str = "broker-order-1",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": broker_id,
        "client_order_id": client_order_id,
        "symbol": symbol,
        "side": side,
        "status": status,
    }
    if qty is not None:
        payload["qty"] = qty
    if filled_qty is not None:
        payload["filled_qty"] = filled_qty
    return payload


def paper_gateway(
    *,
    cash: str | Decimal = "100000",
    positions: tuple[dict[str, object], ...] = (),
    open_orders: tuple[dict[str, object], ...] = (),
    unavailable: bool = False,
    orders_by_client_id: dict[str, dict[str, object] | None] | None = None,
) -> FakeAlpacaGateway:
    return FakeAlpacaGateway(
        account=account_payload(cash=cash),
        positions=positions,
        assets=PHASE1_ASSETS,
        open_orders=open_orders,
        orders_by_client_id=orders_by_client_id or {},
        clock={
            "timestamp": DEFAULT_NOW.isoformat(),
            "is_open": True,
            "next_open": "2026-09-02T13:30:00+00:00",
            "next_close": "2026-09-01T20:00:00+00:00",
        },
        unavailable=unavailable,
    )


def temp_store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "opaca.sqlite")


def b7_cash() -> Decimal:
    evidence = load_evidence("b7_settlement_sell_20260828T135900Z.json")
    observations = cast(dict[str, object], evidence["observations"])
    post = cast(dict[str, object], observations["post_state"])
    account = cast(dict[str, object], post["account"])
    return Decimal(str(account["cash"]))


assert set(PHASE1_ASSETS) >= set(ASSET_UNIVERSE)
