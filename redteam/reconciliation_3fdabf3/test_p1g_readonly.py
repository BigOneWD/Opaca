"""P1-G: read-only Alpaca guarantee, beyond a grep."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import opaca
import pytest
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


def test_g3_no_dynamic_dispatch_reaches_a_broker_object():
    """Every computed getattr/setattr in the package must be self-dispatch or the
    read-only guard iterating its own name set. Asserted semantically: the target
    expression and the enclosing function, never a file:line allowlist."""
    offenders = []
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def enclosing_function(node):
            while node in parents:
                node = parents[node]
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    return node.name
            return "<module>"

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id not in {"getattr", "setattr"} or len(node.args) < 2:
                continue
            name_arg = node.args[1]
            if isinstance(name_arg, ast.Constant):
                continue  # a literal attribute name is not dynamic dispatch
            target = node.args[0]
            target_name = target.id if isinstance(target, ast.Name) else ast.dump(target)
            offenders.append(
                {
                    "module": path.relative_to(ROOT).as_posix(),
                    "function": enclosing_function(node),
                    "target": target_name,
                }
            )

    for offender in offenders:
        # self-dispatch inside the policy engine's own check dispatcher
        if offender["target"] == "self":
            continue
        # the read-only guard walking FORBIDDEN_BROKER_MUTATIONS / dir()
        if offender["module"] == "broker/gateway.py" and offender["target"] in {
            "gateway",
            "gateway_type",
        }:
            continue
        raise AssertionError(f"dynamic dispatch onto a non-self target: {offender}")


def test_g3b_the_read_only_guard_uses_only_literal_attribute_names():
    """mutation.py must not build forbidden names dynamically: a computed name
    there would let the blacklist be bypassed by construction."""
    tree = ast.parse((ROOT / "broker" / "mutation.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "getattr":
                assert len(node.args) >= 2
                assert isinstance(node.args[1], ast.Constant), ast.dump(node)


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
    from tests.state_helpers import PHASE1_ASSETS, account_payload, position_payload

    from probe_support import (
        DEFAULT_NOW,
        DEFAULT_PRICES,
        make_order,
        make_proposal,
        temp_store,
    )

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


def test_no_mutable_trading_client_is_retained():
    """CLOSED at d85a2e6 (was P1-G-1). AlpacaPaperGateway captured the raw
    TradingClient on ``_client``; it now captures bound read callables under
    ``__slots__`` and retains no client attribute."""
    calls = []

    class FakeTradingClient:
        _base_url = "https://paper-api.alpaca.markets"
        _paper = True

        def get_account(self):
            return {"cash": "1"}

        def get_all_positions(self):
            return ()

        def get_asset(self, symbol):
            return {"symbol": symbol}

        def get_orders(self, filter=None):
            return ()

        def get_order_by_client_id(self, client_order_id):
            return None

        def get_calendar(self, filters=None):
            return ()

        def get_clock(self):
            return {}

        def submit_order(self, *a, **kw):
            calls.append("submit_order")

        def cancel_order_by_id(self, *a, **kw):
            calls.append("cancel_order_by_id")

        def close_all_positions(self, *a, **kw):
            calls.append("close_all_positions")

    gateway = AlpacaPaperGateway(FakeTradingClient())
    assert_read_only_gateway(gateway)
    for attribute in ("_client", "client", "_trading_client", "_api"):
        assert getattr(gateway, attribute, None) is None, attribute
    assert not hasattr(gateway, "__dict__"), "the gateway must use __slots__"
    for name in FORBIDDEN_BROKER_MUTATIONS:
        assert getattr(gateway, name, None) is None, name
    assert gateway.get_account() == {"cash": "1"}
    assert calls == [], "no mutation may be invoked"


def test_the_guard_rejects_a_gateway_that_retains_a_mutable_client():
    """CLOSED at d85a2e6. assert_read_only_gateway now inspects the nested client."""
    from opaca.broker.errors import InvalidBrokerStateError

    class TradingClientLike:
        def submit_order(self, *a, **kw):  # pragma: no cover - never invoked
            raise AssertionError("must never be invoked")

    for attribute in ("_client", "client", "_trading_client"):
        holder = type("Holder", (), {})()
        setattr(holder, attribute, TradingClientLike())
        with pytest.raises(InvalidBrokerStateError) as info:
            assert_read_only_gateway(holder)
        assert "mutable broker client" in str(info.value)


def test_forbidden_names_cover_the_real_alpaca_py_surface_and_http_verbs():
    """CLOSED at d85a2e6 (was P1-G-2). The blacklist missed cancel_order_by_id
    and replace_order_by_id, and had no generic HTTP verbs."""
    required = {
        "submit_order",
        "cancel_order",
        "cancel_order_by_id",
        "cancel_orders",
        "replace_order",
        "replace_order_by_id",
        "close_position",
        "close_all_positions",
        "exercise_options_position",
        "post",
        "put",
        "patch",
        "delete",
        "request",
    }
    missing = sorted(required - FORBIDDEN_BROKER_MUTATIONS)
    assert missing == [], missing
    assert FORBIDDEN_BROKER_MUTATIONS.isdisjoint(ALLOWED_GATEWAY_METHODS)


def test_a_generic_http_mutator_on_the_gateway_is_refused():
    from opaca.broker.errors import InvalidBrokerStateError

    for verb in ("post", "put", "patch", "delete", "request"):
        gw = FakeAlpacaGateway(account={}, assets={})
        setattr(gw, verb, lambda *a, **kw: None)
        with pytest.raises(InvalidBrokerStateError):
            assert_read_only_gateway(gw)


def test_FINDING_bound_read_methods_still_expose_their_owner():
    """The retained bound read callables carry ``__self__``, so the TradingClient is
    still reachable one hop away from a gateway that passes the guard.

    Not called here. Python cannot make the owner truly unreachable while bound
    methods are held; the cheap hardening is for assert_read_only_gateway to walk
    ``__self__`` of each retained callable as well as ``_client`` / ``client`` /
    ``_trading_client``.
    """
    calls = []

    class FakeTradingClient:
        _base_url = "https://paper-api.alpaca.markets"
        _paper = True

        def get_account(self):
            return {}

        def get_all_positions(self):
            return ()

        def get_asset(self, symbol):
            return {}

        def get_orders(self, filter=None):
            return ()

        def get_order_by_client_id(self, client_order_id):
            return None

        def get_calendar(self, filters=None):
            return ()

        def get_clock(self):
            return {}

        def submit_order(self, *a, **kw):  # pragma: no cover - never invoked
            calls.append("submit_order")

    gateway = AlpacaPaperGateway(FakeTradingClient())
    assert_read_only_gateway(gateway)
    owner = gateway._get_account.__self__
    reachable = callable(getattr(owner, "submit_order", None))
    assert calls == [], "the probe must not invoke anything"
    assert reachable is True, "probe assumption"
    pytest.fail(
        "FINDING P1-1-r (residual, P3): AlpacaPaperGateway retains bound read "
        "methods, so gateway._get_account.__self__ is the TradingClient and its "
        "mutators remain reachable by introspection. No call site exists; "
        "assert_read_only_gateway does not inspect __self__."
    )
