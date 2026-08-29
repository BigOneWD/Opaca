"""P1-G: read-only Alpaca guarantee, beyond a grep."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import opaca
from opaca.broker.alpaca import AlpacaPaperGateway
from opaca.broker.gateway import (
    ALLOWED_GATEWAY_METHODS,
    AlpacaGateway,
    FakeAlpacaGateway,
    assert_read_only_gateway,
    gateway_methods_are_read_only,
    public_gateway_methods,
)
from opaca.broker.mutation import FORBIDDEN_BROKER_MUTATIONS

ROOT = Path(opaca.__file__).resolve().parent
SOURCES = sorted(ROOT.rglob("*.py"))

MUTATING_NAMES = {
    "submit_order", "cancel_order", "cancel_orders", "replace_order",
    "close_position", "close_all_positions", "exercise_options_position",
    "post", "put", "patch", "delete", "request",
}
HTTP_LIBS = {"requests", "httpx", "urllib", "urllib3", "http", "aiohttp", "socket"}


def test_g1_no_http_client_imports_anywhere_in_the_package():
    offenders = []
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in HTTP_LIBS:
                        offenders.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in HTTP_LIBS:
                    offenders.append(f"{path.name}: from {node.module}")
    assert offenders == [], offenders


def test_g2_no_mutating_attribute_access_or_call_anywhere():
    offenders = []
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in MUTATING_NAMES:
                offenders.append(f"{path.name}:{node.lineno} .{node.attr}")
    # the only permitted mentions are the guard's own literal name set
    assert offenders == [], offenders


def test_g3_no_dynamic_dispatch_onto_a_broker_object():
    """getattr(...) must not be used to reach broker methods by computed name."""
    offenders = []
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"getattr", "setattr"} and node.args:
                    target = node.args[1] if len(node.args) > 1 else None
                    if not isinstance(target, ast.Constant):
                        offenders.append(f"{path.name}:{node.lineno} {node.func.id}(computed)")
    # Allow-list, each hand-audited:
    #   engine.py  - dispatches its own _check_NN handlers by f-string (self, not a broker)
    #   gateway.py - the read-only guard itself iterating FORBIDDEN_BROKER_MUTATIONS / dir()
    allowed = {
        "engine.py:246 getattr(computed)",
        "gateway.py:45 getattr(computed)",
        "gateway.py:107 getattr(computed)",
    }
    assert set(offenders) <= allowed, sorted(set(offenders) - allowed)


def test_g4_alpaca_import_is_lazy_and_trading_only():
    text = (ROOT / "broker" / "alpaca.py").read_text(encoding="utf-8")
    module_level = [
        line for line in text.splitlines()
        if line.startswith("import alpaca") or line.startswith("from alpaca")
    ]
    assert module_level == [], module_level
    assert "OrderRequest" not in text
    assert "MarketOrderRequest" not in text


def test_g5_protocol_surface_has_no_mutators():
    names = {n for n in dir(AlpacaGateway) if not n.startswith("_")}
    assert names & FORBIDDEN_BROKER_MUTATIONS == set()
    assert public_gateway_methods(FakeAlpacaGateway) >= ALLOWED_GATEWAY_METHODS
    assert gateway_methods_are_read_only(FakeAlpacaGateway)
    assert gateway_methods_are_read_only(AlpacaPaperGateway)


def test_g6_guard_rejects_a_gateway_that_exposes_a_mutator():
    from opaca.broker.errors import InvalidBrokerStateError

    class Sneaky(FakeAlpacaGateway):
        def submit_order(self, *a, **kw):  # pragma: no cover - never called
            raise AssertionError("must never be invoked")

    gw = Sneaky(account={}, assets={})
    with pytest.raises(InvalidBrokerStateError):
        assert_read_only_gateway(gw)


def test_g7_orchestrator_rejects_a_gateway_with_a_mutator(tmp_path):
    from opaca.domain.models import Side
    from opaca.orchestration.reserve import read_reconcile_evaluate_reserve
    from opaca.persistence.types import ReconciliationStatus

    from probe_support import DEFAULT_NOW, DEFAULT_PRICES, make_order, make_proposal, temp_store
    from tests.state_helpers import PHASE1_ASSETS, account_payload, position_payload

    class Sneaky(FakeAlpacaGateway):
        def submit_order(self, *a, **kw):  # pragma: no cover
            raise AssertionError("must never be invoked")

    gw = Sneaky(account=account_payload(), positions=(position_payload(qty="100"),),
                assets=PHASE1_ASSETS)
    store = temp_store(tmp_path)
    p = make_proposal("x", [make_order("x", 0, "SGOV", Side.SELL, "10",
                                       DEFAULT_PRICES["SGOV"])])
    recon, out = read_reconcile_evaluate_reserve(store, gw, p, now=DEFAULT_NOW,
                                                 prices=DEFAULT_PRICES)
    assert recon.status is ReconciliationStatus.INVALID_BROKER_STATE
    assert not out.is_auto
    store.close()


def test_g8_paper_endpoint_is_enforced_not_just_the_flag():
    from opaca.broker.errors import PaperEnvironmentError

    class LiveClient:
        _base_url = "https://api.alpaca.markets"
        _paper = True

    class BlankClient:
        _base_url = ""

    class PaperButFlagFalse:
        _base_url = "https://paper-api.alpaca.markets"
        _paper = False

    for client in (LiveClient(), BlankClient(), PaperButFlagFalse()):
        with pytest.raises(PaperEnvironmentError):
            AlpacaPaperGateway(client)


def test_g9_no_credentials_are_logged_or_persisted():
    text = "\n".join(p.read_text(encoding="utf-8") for p in SOURCES)
    assert not re.search(r"print\(.*(secret|key_id|APCA)", text, re.IGNORECASE)
    paper = (ROOT / "broker" / "paper.py").read_text(encoding="utf-8")
    assert "os.environ.get" in paper
    assert "record_audit" not in paper


# --------------------------------------------------------------- FINDING


def test_FINDING_readonly_wrapper_retains_a_fully_mutable_trading_client():
    """AlpacaPaperGateway stores the raw alpaca-py TradingClient on ``_client``.
    assert_read_only_gateway inspects only the wrapper, so every mutation method of
    the wrapped client stays reachable from any holder of the 'read-only' gateway."""
    calls = []

    class FakeTradingClient:
        _base_url = "https://paper-api.alpaca.markets"
        _paper = True

        def submit_order(self, *a, **kw):
            calls.append("submit_order")
            return "SUBMITTED"

        def cancel_order_by_id(self, *a, **kw):
            calls.append("cancel_order_by_id")

        def close_all_positions(self, *a, **kw):
            calls.append("close_all_positions")

    gateway = AlpacaPaperGateway(FakeTradingClient())
    assert_read_only_gateway(gateway)  # passes: the wrapper has no forbidden names
    reachable = [
        name for name in ("submit_order", "cancel_order_by_id", "close_all_positions")
        if callable(getattr(gateway._client, name, None))
    ]
    assert gateway._client.submit_order("anything") == "SUBMITTED"
    pytest.fail(
        "FINDING P1-G-1: AlpacaPaperGateway._client is a fully mutable TradingClient; "
        f"reachable mutation methods via the 'read-only' gateway: {reachable}. "
        "assert_read_only_gateway() only inspects the wrapper's own attributes, and "
        "'cancel_order_by_id' is not even in FORBIDDEN_BROKER_MUTATIONS."
    )


def test_FINDING_forbidden_name_list_misses_real_alpaca_py_method_names():
    """The guard is a name blacklist; alpaca-py's actual method names differ."""
    real_alpaca_mutators = {
        "submit_order",
        "cancel_order_by_id",
        "cancel_orders",
        "replace_order_by_id",
        "close_position",
        "close_all_positions",
        "exercise_options_position",
    }
    missed = sorted(real_alpaca_mutators - FORBIDDEN_BROKER_MUTATIONS)
    assert missed, "probe assumption"
    pytest.fail(
        f"FINDING P1-G-2: FORBIDDEN_BROKER_MUTATIONS does not contain {missed}; "
        "a gateway exposing cancel_order_by_id or replace_order_by_id passes "
        "assert_read_only_gateway()"
    )
