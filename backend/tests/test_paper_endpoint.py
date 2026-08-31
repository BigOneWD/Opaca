"""Exact paper endpoint validation. Parsed host, not prefix matching."""

from __future__ import annotations

import pytest
from opaca.broker.errors import PaperEnvironmentError
from opaca.broker.gateway import PAPER_ENDPOINT, is_exact_paper_endpoint, require_paper_endpoint
from opaca.broker.paper import verify_paper_client
from opaca.execution.gateway import FakePaperExecutionGateway, assert_paper_execution_gateway
from opaca.preflight import _verify_paper_endpoint


class _EndpointGateway:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint


class _Client:
    def __init__(self, url: str, paper: bool = True) -> None:
        self._base_url = url
        self._paper = paper


EXACT = PAPER_ENDPOINT
LOOKALIKE = "https://paper-api.alpaca.markets.evil.com"
LIVE = "https://api.alpaca.markets"
HTTP = "http://paper-api.alpaca.markets"
MALFORMED = "not a url"


@pytest.mark.parametrize(
    "endpoint",
    [
        EXACT,
        EXACT + "/",
    ],
)
def test_exact_paper_endpoint_accepted(endpoint: str) -> None:
    assert is_exact_paper_endpoint(endpoint) is True
    assert require_paper_endpoint(endpoint) == EXACT
    assert_paper_execution_gateway(FakePaperExecutionGateway(endpoint=endpoint))
    assert verify_paper_client(_Client(endpoint)) == EXACT
    assert _verify_paper_endpoint(_EndpointGateway(endpoint)) == EXACT


@pytest.mark.parametrize(
    "endpoint",
    [
        LOOKALIKE,
        "https://evil-paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets.evil.com/v2",
        "https://paper-api.alpaca.markets@evil.com",
        "https://user@paper-api.alpaca.markets",
        "https://user:pass@paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets:8443",
        "https://paper-api.alpaca.markets:443",
        "https://paper-api.alpaca.markets/v2",
        "https://paper-api.alpaca.markets?x=1",
        "https://paper-api.alpaca.markets#frag",
    ],
)
def test_lookalike_host_rejected(endpoint: str) -> None:
    assert is_exact_paper_endpoint(endpoint) is False
    with pytest.raises(PaperEnvironmentError, match="paper endpoint not confirmed"):
        require_paper_endpoint(endpoint)
    with pytest.raises(PaperEnvironmentError):
        assert_paper_execution_gateway(FakePaperExecutionGateway(endpoint=endpoint))
    with pytest.raises(PaperEnvironmentError):
        verify_paper_client(_Client(endpoint))
    with pytest.raises(PaperEnvironmentError):
        _verify_paper_endpoint(_EndpointGateway(endpoint))


def test_live_endpoint_rejected() -> None:
    with pytest.raises(PaperEnvironmentError, match="live Alpaca endpoint is forbidden"):
        require_paper_endpoint(LIVE)
    with pytest.raises(PaperEnvironmentError, match="live"):
        assert_paper_execution_gateway(FakePaperExecutionGateway(endpoint=LIVE))
    with pytest.raises(PaperEnvironmentError, match="live"):
        verify_paper_client(_Client(LIVE, paper=False))
    with pytest.raises(PaperEnvironmentError, match="live"):
        _verify_paper_endpoint(_EndpointGateway(LIVE))


def test_http_scheme_rejected() -> None:
    with pytest.raises(PaperEnvironmentError, match="paper endpoint not confirmed"):
        require_paper_endpoint(HTTP)
    with pytest.raises(PaperEnvironmentError):
        assert_paper_execution_gateway(FakePaperExecutionGateway(endpoint=HTTP))
    with pytest.raises(PaperEnvironmentError):
        verify_paper_client(_Client(HTTP))
    with pytest.raises(PaperEnvironmentError):
        _verify_paper_endpoint(_EndpointGateway(HTTP))


@pytest.mark.parametrize(
    "endpoint",
    [
        MALFORMED,
        "",
        "paper-api.alpaca.markets",
        "https://",
        "https://localhost",
        "http://127.0.0.1",
        "https://127.0.0.1",
    ],
)
def test_malformed_endpoint_rejected(endpoint: str) -> None:
    assert is_exact_paper_endpoint(endpoint) is False
    with pytest.raises(PaperEnvironmentError):
        require_paper_endpoint(endpoint)
    with pytest.raises(PaperEnvironmentError):
        assert_paper_execution_gateway(FakePaperExecutionGateway(endpoint=endpoint))
    with pytest.raises(PaperEnvironmentError):
        verify_paper_client(_Client(endpoint))
    with pytest.raises(PaperEnvironmentError):
        _verify_paper_endpoint(_EndpointGateway(endpoint))
