"""Narrow paper-only mutating gateway. No raw TradingClient. No live endpoint."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, runtime_checkable

from opaca.broker.errors import (
    BrokerUnavailableError,
    InvalidBrokerStateError,
    PaperEnvironmentError,
)
from opaca.broker.gateway import LIVE_ENDPOINT, PAPER_ENDPOINT, BrokerPayload
from opaca.broker.mutation import FORBIDDEN_BROKER_MUTATIONS, nested_mutable_client_method
from opaca.domain.models import Side
from opaca.domain.money import ZERO, non_negative_money, positive_money, round_quantity
from opaca.policy.client_order_id import is_valid_client_order_id

ALLOWED_EXECUTION_METHODS: frozenset[str] = frozenset({"submit_order", "cancel_order_by_id"})


@dataclass(frozen=True)
class PaperOrderRequest:
    symbol: str
    side: Side
    quantity: Decimal
    client_order_id: str
    time_in_force: str = "day"
    order_type: str = "market"
    limit_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        if self.time_in_force != "day":
            raise ValueError("only DAY time_in_force is permitted")
        if self.order_type == "market":
            if self.limit_price is not None:
                raise ValueError("market orders cannot include limit_price")
        elif self.order_type == "limit":
            if self.limit_price is None:
                raise ValueError("limit orders require limit_price")
            object.__setattr__(self, "limit_price", positive_money(self.limit_price))
        else:
            raise ValueError("only market or limit DAY orders are permitted")
        if not is_valid_client_order_id(self.client_order_id):
            raise ValueError("client_order_id violates Alpaca constraints")
        object.__setattr__(self, "quantity", round_quantity(self.quantity))


class DuplicateClientOrderIdError(BrokerUnavailableError):
    """Broker rejected a duplicate client_order_id. Lookup; do not mint a new id."""

    def __init__(self, client_order_id: str) -> None:
        super().__init__(f"client_order_id already exists at broker: {client_order_id}")
        self.client_order_id = client_order_id


@runtime_checkable
class PaperMutatingGateway(Protocol):
    """Exact mutation surface Phase 3 is allowed to use."""

    @property
    def endpoint(self) -> str: ...

    def submit_order(self, request: PaperOrderRequest) -> BrokerPayload: ...

    def cancel_order_by_id(self, broker_order_id: str) -> None: ...


def assert_paper_execution_gateway(gateway: object) -> None:
    endpoint = str(getattr(gateway, "endpoint", ""))
    if endpoint.startswith(LIVE_ENDPOINT):
        raise PaperEnvironmentError("live Alpaca endpoint is forbidden")
    if not endpoint.startswith(PAPER_ENDPOINT):
        raise PaperEnvironmentError(
            f"paper endpoint not confirmed; expected prefix {PAPER_ENDPOINT!r}"
        )
    nested = nested_mutable_client_method(gateway)
    if nested is not None:
        raise InvalidBrokerStateError(
            f"execution gateway retains a mutable broker client exposing {nested}"
        )
    for name in FORBIDDEN_BROKER_MUTATIONS:
        if name in ALLOWED_EXECUTION_METHODS:
            continue
        if callable(getattr(gateway, name, None)):
            raise InvalidBrokerStateError(f"execution gateway exposes forbidden method {name}")
    if not callable(getattr(gateway, "submit_order", None)):
        raise InvalidBrokerStateError("execution gateway missing submit_order")


@dataclass
class FakePaperExecutionGateway:
    """Deterministic offline mutating double. Never talks to Alpaca."""

    endpoint: str = PAPER_ENDPOINT
    orders: dict[str, dict[str, object]] = field(default_factory=dict)
    submit_calls: int = 0
    cancel_calls: int = 0
    timeout_before_accept: bool = False
    timeout_after_accept: bool = False
    reject_reason: str | None = None
    reject_on_call: int | None = None
    fill_on_submit: bool = True
    partial_fill_qty: Decimal | None = None
    next_broker_id: int = 1
    fill_price: Decimal | None = None
    linked_account: dict[str, object] | None = None
    linked_positions: list[dict[str, object]] | None = None
    linked_open_orders: list[dict[str, object]] | None = None

    def submit_order(self, request: PaperOrderRequest) -> BrokerPayload:
        self.submit_calls += 1
        if self.timeout_before_accept:
            raise BrokerUnavailableError("network timeout before broker accept")
        if request.client_order_id in self.orders:
            raise DuplicateClientOrderIdError(request.client_order_id)
        if self.reject_reason is not None or (
            self.reject_on_call is not None and self.submit_calls == self.reject_on_call
        ):
            payload = self._payload(
                request,
                status="rejected",
                filled=ZERO,
                broker_id=f"broker-{self.next_broker_id}",
            )
            self.orders[request.client_order_id] = payload
            self.next_broker_id += 1
            return payload
        filled = ZERO
        status = "new"
        if self.fill_on_submit:
            if self.partial_fill_qty is not None:
                filled = non_negative_money(self.partial_fill_qty)
                if filled >= request.quantity:
                    filled = request.quantity
                    status = "filled"
                elif filled == ZERO:
                    status = "new"
                else:
                    status = "partially_filled"
            else:
                filled = request.quantity
                status = "filled"
        payload = self._payload(
            request,
            status=status,
            filled=filled,
            broker_id=f"broker-{self.next_broker_id}",
        )
        self.orders[request.client_order_id] = payload
        self.next_broker_id += 1
        self._apply_fill_to_linked(request, filled, status)
        if self.timeout_after_accept:
            raise BrokerUnavailableError("network timeout after broker accept")
        return payload

    def cancel_order_by_id(self, broker_order_id: str) -> None:
        self.cancel_calls += 1
        for payload in self.orders.values():
            if str(payload.get("id")) != broker_order_id:
                continue
            status = str(payload.get("status", ""))
            if status in {"filled", "canceled", "cancelled", "rejected", "expired"}:
                return
            payload["status"] = "canceled"
            if self.linked_open_orders is not None:
                self.linked_open_orders[:] = [
                    item
                    for item in self.linked_open_orders
                    if str(item.get("id")) != broker_order_id
                ]
            return
        raise BrokerUnavailableError(f"order {broker_order_id} not found")

    def _payload(
        self,
        request: PaperOrderRequest,
        *,
        status: str,
        filled: Decimal,
        broker_id: str,
    ) -> dict[str, object]:
        price = self.fill_price if self.fill_price is not None else Decimal("100.69")
        payload: dict[str, object] = {
            "id": broker_id,
            "client_order_id": request.client_order_id,
            "symbol": request.symbol,
            "side": request.side.value.lower(),
            "status": status,
            "qty": format(request.quantity, "f"),
            "filled_qty": format(filled, "f"),
            "filled_avg_price": format(price, "f") if filled > ZERO else None,
            "order_type": request.order_type,
            "type": request.order_type,
        }
        if request.limit_price is not None:
            payload["limit_price"] = format(request.limit_price, "f")
        return payload

    def _apply_fill_to_linked(
        self, request: PaperOrderRequest, filled: Decimal, status: str
    ) -> None:
        if filled <= ZERO:
            if status == "new" and self.linked_open_orders is not None:
                self.linked_open_orders.append(self.orders[request.client_order_id])
            return
        price = self.fill_price if self.fill_price is not None else Decimal("100.69")
        notional = filled * price
        if self.linked_account is not None:
            cash = Decimal(str(self.linked_account["cash"]))
            if request.side is Side.BUY:
                cash -= notional
            else:
                cash += notional
            if cash < ZERO:
                cash = ZERO
            self.linked_account["cash"] = format(cash, "f")
        if self.linked_positions is not None:
            found = False
            updated: list[dict[str, object]] = []
            for position in self.linked_positions:
                if str(position["symbol"]) != request.symbol:
                    updated.append(position)
                    continue
                found = True
                qty = Decimal(str(position["qty"]))
                available = Decimal(str(position["qty_available"]))
                if request.side is Side.BUY:
                    qty += filled
                    available += filled
                else:
                    qty -= filled
                    available -= filled
                    if available < ZERO:
                        available = ZERO
                if qty < ZERO:
                    qty = ZERO
                if qty == ZERO:
                    continue
                price_mv = qty * price
                updated.append(
                    {
                        "symbol": request.symbol,
                        "side": "long",
                        "qty": format(qty, "f"),
                        "qty_available": format(available, "f"),
                        "market_value": format(price_mv, "f"),
                    }
                )
            if request.side is Side.BUY and not found:
                updated.append(
                    {
                        "symbol": request.symbol,
                        "side": "long",
                        "qty": format(filled, "f"),
                        "qty_available": format(filled, "f"),
                        "market_value": format(filled * price, "f"),
                    }
                )
            self.linked_positions[:] = updated
        if self.linked_open_orders is not None:
            if status in {"new", "accepted", "partially_filled"}:
                existing = [
                    item
                    for item in self.linked_open_orders
                    if str(item.get("client_order_id")) != request.client_order_id
                ]
                existing.append(self.orders[request.client_order_id])
                self.linked_open_orders[:] = existing
            else:
                self.linked_open_orders[:] = [
                    item
                    for item in self.linked_open_orders
                    if str(item.get("client_order_id")) != request.client_order_id
                ]
