"""Read-only Alpaca gateway protocol and offline fake.

No submit/cancel/replace/close/exercise methods exist on this surface.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

from opaca.broker.errors import (
    BrokerUnavailableError,
    InvalidBrokerStateError,
    PaperEnvironmentError,
)
from opaca.broker.mutation import (
    ALLOWED_GATEWAY_METHODS,
    FORBIDDEN_BROKER_MUTATIONS,
    nested_mutable_client_method,
)

PAPER_ENDPOINT = "https://paper-api.alpaca.markets"
LIVE_ENDPOINT = "https://api.alpaca.markets"
PAPER_HOSTNAME = "paper-api.alpaca.markets"
LIVE_HOSTNAME = "api.alpaca.markets"
ASSET_UNIVERSE: tuple[str, ...] = ("SGOV", "BIL", "SHV")


def _endpoint_parts(
    endpoint: str,
) -> tuple[str, str, int | None, str, str, str, str | None] | None:
    if not endpoint:
        return None
    if "://" not in endpoint:
        return None
    scheme, remainder = endpoint.split("://", 1)
    if not scheme or not remainder:
        return None
    fragment = ""
    if "#" in remainder:
        remainder, fragment = remainder.split("#", 1)
    query = ""
    if "?" in remainder:
        remainder, query = remainder.split("?", 1)
    if "/" in remainder:
        authority, path_rest = remainder.split("/", 1)
        path = "/" + path_rest
    else:
        authority = remainder
        path = ""
    userinfo: str | None
    if "@" in authority:
        userinfo, authority = authority.rsplit("@", 1)
    else:
        userinfo = None
    if not authority or authority.startswith("["):
        return None
    port: int | None
    hostname = authority
    if ":" in authority:
        host, port_text = authority.rsplit(":", 1)
        if not host or not port_text.isdigit():
            return None
        hostname = host
        port = int(port_text)
    else:
        port = None
    if not hostname or ".." in hostname or hostname.startswith(".") or hostname.endswith("."):
        return None
    return (scheme.lower(), hostname.lower(), port, path, query, fragment, userinfo)


def is_live_endpoint(endpoint: str) -> bool:
    parts = _endpoint_parts(endpoint)
    if parts is None:
        return False
    scheme, hostname, _port, _path, _query, _fragment, _userinfo = parts
    return scheme == "https" and hostname == LIVE_HOSTNAME


def is_exact_paper_endpoint(endpoint: str) -> bool:
    parts = _endpoint_parts(endpoint)
    if parts is None:
        return False
    scheme, hostname, port, path, query, fragment, userinfo = parts
    return (
        scheme == "https"
        and hostname == PAPER_HOSTNAME
        and port is None
        and userinfo is None
        and query == ""
        and fragment == ""
        and path in {"", "/"}
    )


def require_paper_endpoint(endpoint: str) -> str:
    if is_live_endpoint(endpoint):
        raise PaperEnvironmentError("live Alpaca endpoint is forbidden")
    if not is_exact_paper_endpoint(endpoint):
        raise PaperEnvironmentError("paper endpoint not confirmed")
    return PAPER_ENDPOINT


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
    endpoint: str = PAPER_ENDPOINT
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
