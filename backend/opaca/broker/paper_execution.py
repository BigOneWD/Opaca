"""Live paper mutating gateway. alpaca-py is imported only when constructed."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, cast

from opaca.broker.adapters import as_mapping
from opaca.broker.errors import BrokerUnavailableError, PaperEnvironmentError
from opaca.broker.gateway import BrokerPayload
from opaca.broker.paper import load_paper_credentials, verify_paper_client
from opaca.execution.gateway import (
    DuplicateClientOrderIdError,
    PaperOrderRequest,
    assert_paper_execution_gateway,
)


class _TradingMutateClient(Protocol):
    def submit_order(self, order_data: object = ...) -> object: ...

    def cancel_order_by_id(self, order_id: object) -> object: ...


class AlpacaPaperExecutionGateway:
    """Narrow paper mutation surface. The mutable TradingClient is never retained."""

    __slots__ = ("_endpoint", "_submit_order", "_cancel_order_by_id")

    def __init__(self, client: object) -> None:
        endpoint = verify_paper_client(client)
        mutator = cast(_TradingMutateClient, client)
        submit_order: Callable[..., object] = mutator.submit_order
        cancel_order_by_id: Callable[..., object] = mutator.cancel_order_by_id
        self._endpoint = endpoint
        self._submit_order = submit_order
        self._cancel_order_by_id = cancel_order_by_id
        assert_paper_execution_gateway(self)

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def submit_order(self, request: PaperOrderRequest) -> BrokerPayload:
        try:
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

            side = OrderSide.BUY if request.side.value == "BUY" else OrderSide.SELL
            if request.order_type == "limit":
                if request.limit_price is None:
                    raise PaperEnvironmentError("limit order missing limit_price")
                order_data: object = LimitOrderRequest(
                    symbol=request.symbol,
                    qty=format(request.quantity, "f"),
                    side=side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=format(request.limit_price, "f"),
                    client_order_id=request.client_order_id,
                )
            else:
                order_data = MarketOrderRequest(
                    symbol=request.symbol,
                    qty=format(request.quantity, "f"),
                    side=side,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=request.client_order_id,
                )
            raw = self._submit_order(order_data=order_data)
        except PaperEnvironmentError:
            raise
        except Exception as exc:
            message = str(exc).lower()
            if "client_order_id" in message and "unique" in message:
                raise DuplicateClientOrderIdError(request.client_order_id) from exc
            raise BrokerUnavailableError("submit_order failed") from exc
        return as_mapping(raw)

    def cancel_order_by_id(self, broker_order_id: str) -> None:
        try:
            self._cancel_order_by_id(broker_order_id)
        except Exception as exc:
            raise BrokerUnavailableError("cancel_order_by_id failed") from exc


def open_paper_execution_gateway_from_env() -> AlpacaPaperExecutionGateway:
    """Construct a paper-only mutating gateway from process environment credentials."""
    try:
        from alpaca.trading.client import TradingClient
    except ImportError as exc:
        raise PaperEnvironmentError("alpaca-py is required for live paper mutation") from exc
    key_id, secret = load_paper_credentials()
    client: Any = TradingClient(api_key=key_id, secret_key=secret, paper=True)
    return AlpacaPaperExecutionGateway(client)
