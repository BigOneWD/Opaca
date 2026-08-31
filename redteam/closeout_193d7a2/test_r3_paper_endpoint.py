"""P2-1 retest: exact parsed paper-endpoint validation in every production guard."""
from __future__ import annotations

import ast
import os
import pathlib

import pytest
from opaca.broker.errors import PaperEnvironmentError
from opaca.broker.gateway import (
    PAPER_ENDPOINT,
    is_exact_paper_endpoint,
    is_live_endpoint,
    require_paper_endpoint,
)
from opaca.broker.paper import verify_paper_client
from opaca.execution.gateway import assert_paper_execution_gateway
from opaca.execution.service import _forbid_live_endpoint
from opaca.preflight import _verify_paper_endpoint

from closeout_support import DEFAULT_NOW, StubTradingClient, buy_setup, world

PKG = pathlib.Path(os.environ["OPACA_BACKEND"]) / "opaca"

ACCEPT = [
    "https://paper-api.alpaca.markets",
    "https://paper-api.alpaca.markets/",
    "https://PAPER-API.ALPACA.MARKETS",
]

REJECT = [
    # look-alike hosts (the previous review's live finding)
    "https://paper-api.alpaca.markets.evil.com",
    "https://paper-api.alpaca.markets.evil.com/v2",
    "https://evil-paper-api.alpaca.markets",
    "https://paper-api.alpaca.markets.co",
    "https://xpaper-api.alpaca.markets",
    "https://paper-api-alpaca.markets",
    "https://sub.paper-api.alpaca.markets",
    "https://paper-api.alpaca.markets.",
    "https://paper-api..alpaca.markets",
    # live
    "https://api.alpaca.markets",
    "https://api.alpaca.markets/v2",
    "http://api.alpaca.markets",
    # scheme
    "http://paper-api.alpaca.markets",
    "ftp://paper-api.alpaca.markets",
    "//paper-api.alpaca.markets",
    "paper-api.alpaca.markets",
    "https:/paper-api.alpaca.markets",
    # localhost / private
    "http://localhost",
    "https://localhost",
    "http://localhost:8080",
    "http://127.0.0.1",
    "https://127.0.0.1:443",
    "http://0.0.0.0",
    "https://[::1]",
    # userinfo tricks
    "https://paper-api.alpaca.markets@evil.com",
    "https://evil.com@paper-api.alpaca.markets",
    "https://user:pass@paper-api.alpaca.markets",
    "https://paper-api.alpaca.markets@paper-api.alpaca.markets.evil.com",
    # ports
    "https://paper-api.alpaca.markets:443",
    "https://paper-api.alpaca.markets:8443",
    "https://paper-api.alpaca.markets:0",
    "https://paper-api.alpaca.markets:",
    # path / query / fragment
    "https://paper-api.alpaca.markets/v2",
    "https://paper-api.alpaca.markets/../evil",
    "https://paper-api.alpaca.markets?x=1",
    "https://paper-api.alpaca.markets#evil.com",
    "https://paper-api.alpaca.markets/@evil.com",
    "https://paper-api.alpaca.markets\\@evil.com",
    "https://paper-api.alpaca.markets\\.evil.com",
    # malformed / empty
    "",
    "   ",
    " https://paper-api.alpaca.markets",
    "https://paper-api.alpaca.markets ",
    "https://paper-api.alpaca.markets\n",
    "https://paper-api.alpaca.markets\t",
    "https://",
    "://paper-api.alpaca.markets",
    "https:///paper-api.alpaca.markets",
    "https://:443",
]


class _Gateway:
    def __init__(self, endpoint):
        self.endpoint = endpoint

    def submit_order(self, request):  # pragma: no cover - never reached
        raise AssertionError("submit_order must never be reached in an endpoint probe")

    def cancel_order_by_id(self, broker_order_id):  # pragma: no cover
        raise AssertionError("cancel must never be reached in an endpoint probe")


GUARDS = {
    "require_paper_endpoint": lambda url: require_paper_endpoint(url),
    "broker.paper.verify_paper_client": lambda url: verify_paper_client(
        StubTradingClient(base_url=url)),
    "execution.gateway.assert_paper_execution_gateway":
        lambda url: assert_paper_execution_gateway(_Gateway(url)),
    "execution.service._forbid_live_endpoint":
        lambda url: _forbid_live_endpoint(_Gateway(url)),
    "preflight._verify_paper_endpoint": lambda url: _verify_paper_endpoint(_Gateway(url)),
}


@pytest.mark.parametrize("guard", sorted(GUARDS))
@pytest.mark.parametrize("url", ACCEPT)
def test_the_documented_paper_endpoint_is_accepted(guard, url):
    GUARDS[guard](url)


@pytest.mark.parametrize("guard", sorted(GUARDS))
@pytest.mark.parametrize("url", REJECT)
def test_every_guard_rejects_non_canonical_endpoints(guard, url):
    with pytest.raises(PaperEnvironmentError):
        GUARDS[guard](url)


def test_predicates_agree_with_the_guards():
    assert is_exact_paper_endpoint(PAPER_ENDPOINT) is True
    assert is_live_endpoint("https://api.alpaca.markets") is True
    for url in REJECT:
        assert is_exact_paper_endpoint(url) is False, url
    assert require_paper_endpoint("https://paper-api.alpaca.markets/") == PAPER_ENDPOINT


def test_no_production_guard_uses_a_string_prefix_endpoint_test():
    """No `startswith` anywhere near an endpoint constant."""
    offenders = []
    for path in PKG.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "startswith"):
                text = ast.unparse(node)
                if any(tok in text for tok in
                       ("ENDPOINT", "endpoint", "base_url", "alpaca.markets", "url")):
                    offenders.append(f"{path.name}:{node.lineno} {text}")
    assert offenders == [], offenders


def test_a_look_alike_endpoint_cannot_mutate_the_broker(tmp_path, monkeypatch):
    """End-to-end: the execution service refuses before any submit."""
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w, pid="endpoint-1")
    assert out.is_auto is True
    from opaca.execution.service import execute_reserved_proposal
    for url in ("https://paper-api.alpaca.markets.evil.com",
                "https://api.alpaca.markets",
                "http://paper-api.alpaca.markets",
                "https://paper-api.alpaca.markets:8443"):
        gateway = _Gateway(url)
        with pytest.raises(PaperEnvironmentError):
            execute_reserved_proposal(
                w.store, w.read(), gateway, proposal, now=DEFAULT_NOW, prices=prices,
                price_bindings=bindings)
    assert w.store.list_execution_orders(proposal_id=proposal.proposal_id) == ()
    w.close()


def test_the_real_gateway_cannot_be_constructed_on_a_look_alike_host():
    from opaca.broker.paper_execution import AlpacaPaperExecutionGateway
    for url in ("https://paper-api.alpaca.markets.evil.com",
                "https://api.alpaca.markets",
                "http://paper-api.alpaca.markets"):
        with pytest.raises(PaperEnvironmentError):
            AlpacaPaperExecutionGateway(StubTradingClient(base_url=url))


def test_the_paper_flag_alone_cannot_rescue_a_bad_endpoint():
    with pytest.raises(PaperEnvironmentError):
        verify_paper_client(StubTradingClient(
            base_url="https://paper-api.alpaca.markets.evil.com", paper=True))
