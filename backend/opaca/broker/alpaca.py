"""Live paper read-only gateway. alpaca-py is imported only when constructed."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from opaca.broker.adapters import as_mapping
from opaca.broker.errors import BrokerUnavailableError, PaperEnvironmentError
from opaca.broker.gateway import BrokerPayload, assert_read_only_gateway
from opaca.broker.paper import load_paper_credentials, verify_paper_client


class AlpacaPaperGateway:
    """Read-only wrapper around alpaca-py TradingClient (paper endpoint only)."""

    def __init__(self, client: object) -> None:
        self._endpoint = verify_paper_client(client)
        self._client = client
        assert_read_only_gateway(self)

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def get_account(self) -> BrokerPayload:
        try:
            return as_mapping(self._client.get_account())  # type: ignore[attr-defined]
        except PaperEnvironmentError:
            raise
        except Exception as exc:
            raise BrokerUnavailableError("get_account failed") from exc

    def get_positions(self) -> Sequence[BrokerPayload]:
        try:
            positions = self._client.get_all_positions()  # type: ignore[attr-defined]
        except Exception as exc:
            raise BrokerUnavailableError("get_positions failed") from exc
        return tuple(as_mapping(item) for item in positions)

    def get_asset(self, symbol: str) -> BrokerPayload:
        try:
            return as_mapping(self._client.get_asset(symbol))  # type: ignore[attr-defined]
        except Exception as exc:
            raise BrokerUnavailableError(f"get_asset({symbol}) failed") from exc

    def get_open_orders(self) -> Sequence[BrokerPayload]:
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest

            request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            orders = self._client.get_orders(filter=request)  # type: ignore[attr-defined]
        except Exception as exc:
            raise BrokerUnavailableError("get_open_orders failed") from exc
        return tuple(as_mapping(item) for item in orders)

    def get_order_by_client_id(self, client_order_id: str) -> BrokerPayload | None:
        try:
            order = self._client.get_order_by_client_id(client_order_id)  # type: ignore[attr-defined]
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
            sessions = self._client.get_calendar(filters=request)  # type: ignore[attr-defined]
        except TypeError:
            try:
                sessions = self._client.get_calendar()  # type: ignore[attr-defined]
            except Exception as exc:
                raise BrokerUnavailableError("get_calendar failed") from exc
        except Exception as exc:
            raise BrokerUnavailableError("get_calendar failed") from exc
        return tuple(as_mapping(item) for item in sessions)

    def get_clock(self) -> BrokerPayload:
        try:
            return as_mapping(self._client.get_clock())  # type: ignore[attr-defined]
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
