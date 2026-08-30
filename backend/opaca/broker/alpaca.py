"""Live paper read-only gateway. alpaca-py is imported only when constructed."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from typing import Any, Protocol, cast

from opaca.broker.adapters import as_mapping
from opaca.broker.errors import BrokerUnavailableError, PaperEnvironmentError
from opaca.broker.gateway import BrokerPayload, assert_read_only_gateway
from opaca.broker.paper import load_paper_credentials, verify_paper_client


class _TradingReadClient(Protocol):
    def get_account(self) -> object: ...

    def get_all_positions(self) -> Sequence[object]: ...

    def get_asset(self, symbol: str) -> object: ...

    def get_orders(self, filter: object = ...) -> Sequence[object]: ...

    def get_order_by_client_id(self, client_order_id: str) -> object: ...

    def get_calendar(self, filters: object = ...) -> Sequence[object]: ...

    def get_clock(self) -> object: ...


class AlpacaPaperGateway:
    """Read-only paper gateway. The mutable TradingClient is never retained.

    Bound read callables are captured at construction; no ``_client`` /
    ``client`` / ``_trading_client`` attribute exists on the instance.
    """

    __slots__ = (
        "_endpoint",
        "_get_account",
        "_get_all_positions",
        "_get_asset",
        "_get_orders",
        "_get_order_by_client_id",
        "_get_calendar",
        "_get_clock",
    )

    def __init__(self, client: object) -> None:
        endpoint = verify_paper_client(client)
        reader = cast(_TradingReadClient, client)
        get_account: Callable[[], object] = reader.get_account
        get_all_positions: Callable[[], Sequence[object]] = reader.get_all_positions
        get_asset: Callable[[str], object] = reader.get_asset
        get_orders: Callable[..., Sequence[object]] = reader.get_orders
        get_order_by_client_id: Callable[[str], object] = reader.get_order_by_client_id
        get_calendar: Callable[..., Sequence[object]] = reader.get_calendar
        get_clock: Callable[[], object] = reader.get_clock
        self._endpoint = endpoint
        self._get_account = get_account
        self._get_all_positions = get_all_positions
        self._get_asset = get_asset
        self._get_orders = get_orders
        self._get_order_by_client_id = get_order_by_client_id
        self._get_calendar = get_calendar
        self._get_clock = get_clock
        assert_read_only_gateway(self)

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def get_account(self) -> BrokerPayload:
        try:
            return as_mapping(self._get_account())
        except PaperEnvironmentError:
            raise
        except Exception as exc:
            raise BrokerUnavailableError("get_account failed") from exc

    def get_positions(self) -> Sequence[BrokerPayload]:
        try:
            positions = self._get_all_positions()
        except Exception as exc:
            raise BrokerUnavailableError("get_positions failed") from exc
        return tuple(as_mapping(item) for item in positions)

    def get_asset(self, symbol: str) -> BrokerPayload:
        try:
            return as_mapping(self._get_asset(symbol))
        except Exception as exc:
            raise BrokerUnavailableError(f"get_asset({symbol}) failed") from exc

    def get_open_orders(self) -> Sequence[BrokerPayload]:
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest

            request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            orders = self._get_orders(filter=request)
        except Exception as exc:
            raise BrokerUnavailableError("get_open_orders failed") from exc
        return tuple(as_mapping(item) for item in orders)

    def get_order_by_client_id(self, client_order_id: str) -> BrokerPayload | None:
        try:
            order = self._get_order_by_client_id(client_order_id)
        except Exception as exc:
            message = str(exc).lower()
            if "not found" in message or "404" in message or "does not exist" in message:
                return None
            raise BrokerUnavailableError("get_order_by_client_id failed") from exc
        return as_mapping(order)

    def get_calendar(self, start: date, end: date) -> Sequence[BrokerPayload]:
        try:
            from alpaca.trading.requests import GetCalendarRequest

            request = GetCalendarRequest(start=start, end=end)
            sessions = self._get_calendar(filters=request)
        except TypeError:
            try:
                sessions = self._get_calendar()
            except Exception as exc:
                raise BrokerUnavailableError("get_calendar failed") from exc
        except Exception as exc:
            raise BrokerUnavailableError("get_calendar failed") from exc
        return tuple(as_mapping(item) for item in sessions)

    def get_clock(self) -> BrokerPayload:
        try:
            return as_mapping(self._get_clock())
        except Exception as exc:
            raise BrokerUnavailableError("get_clock failed") from exc


def open_paper_gateway_from_env() -> AlpacaPaperGateway:
    """Construct a paper-only gateway from process environment credentials."""
    try:
        from alpaca.trading.client import TradingClient
    except ImportError as exc:
        raise PaperEnvironmentError("alpaca-py is required for live paper reads") from exc
    key_id, secret = load_paper_credentials()
    client: Any = TradingClient(api_key=key_id, secret_key=secret, paper=True)
    return AlpacaPaperGateway(client)
