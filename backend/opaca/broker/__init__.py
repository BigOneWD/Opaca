"""Read-only Alpaca gateway. No mutation methods exist on this surface."""

from opaca.broker.errors import (
    BrokerError,
    BrokerUnavailableError,
    InvalidBrokerStateError,
    PaperEnvironmentError,
)
from opaca.broker.gateway import (
    ASSET_UNIVERSE,
    PAPER_ENDPOINT,
    AlpacaGateway,
    FakeAlpacaGateway,
    ReadOnlyAlpacaGateway,
    assert_read_only_gateway,
)
from opaca.broker.mutation import FORBIDDEN_BROKER_MUTATIONS

__all__ = [
    "ASSET_UNIVERSE",
    "FORBIDDEN_BROKER_MUTATIONS",
    "PAPER_ENDPOINT",
    "AlpacaGateway",
    "BrokerError",
    "BrokerUnavailableError",
    "FakeAlpacaGateway",
    "InvalidBrokerStateError",
    "PaperEnvironmentError",
    "ReadOnlyAlpacaGateway",
    "assert_read_only_gateway",
]
