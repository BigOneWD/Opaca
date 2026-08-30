"""Read-only Alpaca gateway protocol and offline fake.

No submit/cancel/replace/close/exercise methods exist on this surface.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

from opaca.broker.errors import BrokerUnavailableError, InvalidBrokerStateError
from opaca.broker.mutation import (
    ALLOWED_GATEWAY_METHODS,
    FORBIDDEN_BROKER_MUTATIONS,
    nested_mutable_client_method,
)

PAPER_ENDPOINT = "https://paper-api.alpaca.markets"
LIVE_ENDPOINT = "https://api.alpaca.markets"
ASSET_UNIVERSE: tuple[str, ...] = ("SGOV", "BIL", "SHV")

BrokerPayload = Mapping[str, object]


@runtime_checkable
class ReadOnlyAlpacaGateway(Protocol):
    """Narrow read-only broker capabilities for this phase.

    Application code receives this protocol only. There is no submit, cancel,
    replace, close, exercise, raw TradingClient, or generic HTTP mutator.
    """

    def get_account(self) -> BrokerPayload: ...

    def get_positions(self) -> Sequence[BrokerPayload]: ...

    def get_asset(self, symbol: str) -> BrokerPayload: ...

    def get_open_orders(self) -> Sequence[BrokerPayload]: ...

    def get_order_by_client_id(self, client_order_id: str) -> BrokerPayload | None: ...

    def get_calendar(self, start: date, end: date) -> Sequence[BrokerPayload]: ...

    def get_clock(self) -> BrokerPayload: ...


AlpacaGateway = ReadOnlyAlpacaGateway


def assert_read_only_gateway(gateway: object) -> None:
    """Fail closed if a gateway instance exposes a mutation method."""
    for name in FORBIDDEN_BROKER_MUTATIONS:
        if callable(getattr(gateway, name, None)):
            raise InvalidBrokerStateError(f"gateway exposes forbidden method {name}")
    nested = nested_mutable_client_method(gateway)
    if nested is not None:
        raise InvalidBrokerStateError(f"gateway retains a mutable broker client exposing {nested}")


@dataclass
class FakeAlpacaGateway:
    """Deterministic offline double. Never talks to Alpaca."""

    account: BrokerPayload
    positions: tuple[BrokerPayload, ...] = ()
    assets: Mapping[str, BrokerPayload] = field(default_factory=dict)
    open_orders: tuple[BrokerPayload, ...] = ()
    orders_by_client_id: Mapping[str, BrokerPayload | None] = field(default_factory=dict)
    calendar: tuple[BrokerPayload, ...] = ()
    clock: BrokerPayload = field(default_factory=dict)
    unavailable: bool = False
    lookup_unavailable: bool = False

    def _require_available(self) -> None:
        if self.unavailable:
            raise BrokerUnavailableError("broker unavailable")

    def get_account(self) -> BrokerPayload:
        self._require_available()
        return self.account

    def get_positions(self) -> Sequence[BrokerPayload]:
        self._require_available()
        return self.positions

    def get_asset(self, symbol: str) -> BrokerPayload:
        self._require_available()
        if symbol not in self.assets:
            raise InvalidBrokerStateError(f"asset metadata missing for {symbol}")
        return self.assets[symbol]

    def get_open_orders(self) -> Sequence[BrokerPayload]:
        self._require_available()
        return self.open_orders

    def get_order_by_client_id(self, client_order_id: str) -> BrokerPayload | None:
        self._require_available()
        if self.lookup_unavailable:
            raise BrokerUnavailableError("order lookup unavailable")
        if client_order_id in self.orders_by_client_id:
            return self.orders_by_client_id[client_order_id]
        return None

    def get_calendar(self, start: date, end: date) -> Sequence[BrokerPayload]:
        self._require_available()
        del start, end
        return self.calendar

    def get_clock(self) -> BrokerPayload:
        self._require_available()
        return self.clock


def public_gateway_methods(gateway_type: type[object]) -> frozenset[str]:
    names = {
        name
        for name in dir(gateway_type)
        if not name.startswith("_") and callable(getattr(gateway_type, name, None))
    }
    return frozenset(names)


def gateway_methods_are_read_only(gateway_type: type[object]) -> bool:
    names = public_gateway_methods(gateway_type)
    if names & FORBIDDEN_BROKER_MUTATIONS:
        return False
    return names >= ALLOWED_GATEWAY_METHODS
