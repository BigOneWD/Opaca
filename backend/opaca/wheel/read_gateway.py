"""Narrow authoritative read-only Alpaca option gateway."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol, cast, runtime_checkable

from opaca.broker.adapters import as_mapping
from opaca.broker.errors import BrokerUnavailableError, InvalidBrokerStateError

BrokerPayload = Mapping[str, object]


class _TradingReadClient(Protocol):
    def get_account(self) -> object: ...

    def get_option_contract(self, symbol_or_id: str) -> object: ...

    def get_option_contracts(self, request: object) -> object: ...

    def get_all_positions(self) -> Sequence[object]: ...

    def get_orders(self, filter: object = ...) -> Sequence[object]: ...

    def get_order_by_client_id(self, client_order_id: str) -> object: ...

    def get_clock(self) -> object: ...


class _OptionDataReadClient(Protocol):
    def get_option_latest_quote(self, request: object) -> object: ...


def _payloads(raw: object, *, response_key: str | None = None) -> list[BrokerPayload]:
    value = raw
    if response_key is not None:
        data = as_mapping(raw)
        value = data.get(response_key)
        if value is None:
            raise InvalidBrokerStateError(f"missing broker response field {response_key!r}")
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InvalidBrokerStateError("broker response is not a sequence")
    return [as_mapping(item) for item in value]


def _position_asset_class(payload: BrokerPayload) -> str:
    value = payload.get("asset_class")
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value.lower()
    return value.lower() if isinstance(value, str) else ""


def _quote_payload(raw: object, symbol: str) -> BrokerPayload:
    data = as_mapping(raw)
    if "bid_price" in data or "bid" in data:
        return data
    value = data.get(symbol)
    if value is None:
        raise InvalidBrokerStateError("option quote missing requested symbol")
    return as_mapping(value)


@runtime_checkable
class OptionReadGateway(Protocol):
    """Only the V1 authoritative broker reads; no mutation capability."""

    def get_account(self) -> BrokerPayload: ...

    def get_option_contracts(self, underlying: str) -> Sequence[BrokerPayload]: ...

    def get_option_contract(self, occ_symbol: str) -> BrokerPayload: ...

    def get_option_quote(self, occ_symbol: str) -> BrokerPayload: ...

    def get_option_positions(self) -> Sequence[BrokerPayload]: ...

    def get_equity_positions(self) -> Sequence[BrokerPayload]: ...

    def get_open_orders(self) -> Sequence[BrokerPayload]: ...

    def get_order_by_client_id(self, client_order_id: str) -> BrokerPayload | None: ...

    def get_clock(self) -> BrokerPayload: ...


class AlpacaOptionReadGateway:
    """Read-only facade over injected alpaca-py trading/data clients.

    Only bound read callables are retained. The mutable clients themselves are
    not stored on the gateway instance.
    """

    __slots__ = (
        "_get_account",
        "_get_option_contract",
        "_get_option_contracts",
        "_get_all_positions",
        "_get_orders",
        "_get_order_by_client_id",
        "_get_clock",
        "_get_option_latest_quote",
    )

    def __init__(self, *, trading_client: object, option_data_client: object) -> None:
        trading = cast(_TradingReadClient, trading_client)
        option_data = cast(_OptionDataReadClient, option_data_client)
        self._get_account: Callable[[], object] = trading.get_account
        self._get_option_contract: Callable[[str], object] = trading.get_option_contract
        self._get_option_contracts: Callable[[object], object] = trading.get_option_contracts
        self._get_all_positions: Callable[[], Sequence[object]] = trading.get_all_positions
        self._get_orders: Callable[..., Sequence[object]] = trading.get_orders
        self._get_order_by_client_id: Callable[[str], object] = trading.get_order_by_client_id
        self._get_clock: Callable[[], object] = trading.get_clock
        self._get_option_latest_quote: Callable[[object], object] = (
            option_data.get_option_latest_quote
        )

    def get_account(self) -> BrokerPayload:
        try:
            return as_mapping(self._get_account())
        except Exception as exc:
            raise BrokerUnavailableError("get_account failed") from exc

    def get_option_contracts(self, underlying: str) -> Sequence[BrokerPayload]:
        try:
            from alpaca.trading.requests import GetOptionContractsRequest

            request = GetOptionContractsRequest(underlying_symbols=[underlying])
            response = self._get_option_contracts(request)
            if isinstance(response, Mapping) or hasattr(response, "model_dump"):
                return _payloads(response, response_key="option_contracts")
            return _payloads(response)
        except InvalidBrokerStateError:
            raise
        except Exception as exc:
            raise BrokerUnavailableError("get_option_contracts failed") from exc

    def get_option_contract(self, occ_symbol: str) -> BrokerPayload:
        try:
            return as_mapping(self._get_option_contract(occ_symbol))
        except Exception as exc:
            raise BrokerUnavailableError("get_option_contract failed") from exc

    def get_option_quote(self, occ_symbol: str) -> BrokerPayload:
        try:
            from alpaca.data.requests import OptionLatestQuoteRequest

            request = OptionLatestQuoteRequest(symbol_or_symbols=occ_symbol)
            return _quote_payload(self._get_option_latest_quote(request), occ_symbol)
        except InvalidBrokerStateError:
            raise
        except Exception as exc:
            raise BrokerUnavailableError("get_option_quote failed") from exc

    def get_option_positions(self) -> Sequence[BrokerPayload]:
        return self._filtered_positions("us_option")

    def get_equity_positions(self) -> Sequence[BrokerPayload]:
        return self._filtered_positions("us_equity")

    def _filtered_positions(self, asset_class: str) -> Sequence[BrokerPayload]:
        try:
            positions = [as_mapping(item) for item in self._get_all_positions()]
            return [item for item in positions if _position_asset_class(item) == asset_class]
        except InvalidBrokerStateError:
            raise
        except Exception as exc:
            raise BrokerUnavailableError("get_positions failed") from exc

    def get_open_orders(self) -> Sequence[BrokerPayload]:
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest

            request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            return [as_mapping(item) for item in self._get_orders(filter=request)]
        except InvalidBrokerStateError:
            raise
        except Exception as exc:
            raise BrokerUnavailableError("get_open_orders failed") from exc

    def get_order_by_client_id(self, client_order_id: str) -> BrokerPayload | None:
        try:
            result = self._get_order_by_client_id(client_order_id)
        except Exception as exc:
            message = str(exc).lower()
            if "not found" in message or "404" in message or "does not exist" in message:
                return None
            raise BrokerUnavailableError("get_order_by_client_id failed") from exc
        if result is None:
            return None
        return as_mapping(result)

    def get_clock(self) -> BrokerPayload:
        try:
            return as_mapping(self._get_clock())
        except Exception as exc:
            raise BrokerUnavailableError("get_clock failed") from exc
