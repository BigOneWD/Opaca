"""S8/S9: mutation surface and Phase 3 execution regressions under LIMIT orders."""
from __future__ import annotations

import ast
import os
import pathlib
import threading
from decimal import Decimal

import pytest
from opaca.broker.errors import PaperEnvironmentError
from opaca.broker.gateway import LIVE_ENDPOINT
from opaca.execution.states import ExecutionState
from opaca.execution.service import execute_reserved_proposal, recover_proposal
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.store import SQLiteStore
from opaca.reconciliation.service import reconcile

from support import DEFAULT_NOW, bind_buy, bind_sell, bind_single_leg_proposal, quotes_for, world

PKG = pathlib.Path(os.environ["OPACA_BACKEND"]) / "opaca"
PROD = sorted(PKG.rglob("*.py"))
MUTATORS = {"submit_order", "cancel_order_by_id", "cancel_order", "close_position",
            "close_all_positions", "replace_order", "replace_order_by_id",
            "delete_order", "cancel_orders", "liquidate", "exercise_options_position",
            "post", "put", "patch", "delete", "request"}


# ------------------------------------------------------------------ S9


def test_the_mutation_surface_is_two_call_sites_on_the_paper_gateway():
    sites = []
    for m in PROD:
        tree = ast.parse(m.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in MUTATORS:
                sites.append((m.relative_to(PKG).as_posix(), node.lineno,
                              ast.unparse(node.func)))
    assert {s[0] for s in sites} == {"execution/service.py"}, sites
    assert {s[2].split(".")[0] for s in sites} == {"mutate_gateway"}, sites
    assert len(sites) == 2, sites


def test_no_production_module_constructs_a_trading_client_outside_the_paper_gateway():
    offenders = []
    for m in PROD:
        tree = ast.parse(m.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("alpaca"):
                if "TradingClient" in {a.name for a in node.names}:
                    offenders.append(m.relative_to(PKG).as_posix())
    assert set(offenders) <= {"broker/alpaca.py", "broker/paper_execution.py"}, offenders


def test_the_live_endpoint_constant_appears_only_in_guards():
    users = []
    for m in PROD:
        text = m.read_text(encoding="utf-8")
        if "LIVE_ENDPOINT" in text and m.name != "gateway.py":
            users.append(m.relative_to(PKG).as_posix())
    assert set(users) <= {"execution/service.py", "execution/gateway.py",
                          "broker/paper.py", "preflight.py"}, users


def test_a_live_endpoint_gateway_can_never_reach_submit(tmp_path):
    w = world(tmp_path, qty="100", cash="100000")
    quotes = quotes_for()
    bound = bind_sell(quotes["SGOV"], Decimal("10"))
    proposal, prices, bindings = bind_single_leg_proposal("live1", bound, quotes)
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    assert evaluate_and_reserve(
        w.store, proposal, now=DEFAULT_NOW, prices=prices,
        expected_snapshot_version=recon.snapshot.version,
        price_bindings=bindings).is_auto is True
    live = w.mutate()
    live.endpoint = LIVE_ENDPOINT
    with pytest.raises(PaperEnvironmentError):
        execute_reserved_proposal(w.store, w.read(), live, proposal, now=DEFAULT_NOW,
                                  prices=prices, price_bindings=bindings)
    assert live.submit_calls == 0
    w.close()


def test_zero_bare_asserts_in_production():
    bad = []
    for m in PROD:
        for node in ast.walk(ast.parse(m.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assert):
                bad.append((m.relative_to(PKG).as_posix(), node.lineno))
    assert bad == []


# ------------------------------------------------------------------ S8


def _auto_sell(w, pid, qty="10"):
    quotes = quotes_for()
    bound = bind_sell(quotes["SGOV"], Decimal(qty))
    proposal, prices, bindings = bind_single_leg_proposal(pid, bound, quotes)
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    out = evaluate_and_reserve(
        w.store, proposal, now=DEFAULT_NOW, prices=prices,
        expected_snapshot_version=recon.snapshot.version, price_bindings=bindings)
    assert out.is_auto is True, out.block_reason
    return proposal, prices, bindings


def test_duplicate_order_prevention_under_limit_orders(tmp_path):
    w = world(tmp_path, qty="100", cash="100000")
    proposal, prices, bindings = _auto_sell(w, "dup1")
    mutate = w.mutate()
    for _ in range(5):
        execute_reserved_proposal(w.store, w.read(), mutate, proposal, now=DEFAULT_NOW,
                                  prices=prices, price_bindings=bindings)
    assert mutate.submit_calls == 1
    assert len(w.store.list_execution_orders(proposal_id="dup1")) == 1
    w.close()


def test_concurrent_executors_submit_once(tmp_path):
    w = world(tmp_path, qty="100", cash="100000")
    proposal, prices, bindings = _auto_sell(w, "conc1")
    mutate = w.mutate()
    barrier = threading.Barrier(4)
    errors = []

    def go():
        store = SQLiteStore(tmp_path / "opaca.sqlite")
        try:
            barrier.wait()
            execute_reserved_proposal(store, w.read(), mutate, proposal, now=DEFAULT_NOW,
                                      prices=prices, price_bindings=bindings)
        except Exception as exc:              # noqa: BLE001
            errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert mutate.submit_calls == 1, errors
    assert len(w.store.list_execution_orders(proposal_id="conc1")) == 1
    w.close()


def test_deterministic_client_order_id_is_reused_not_reminted(tmp_path):
    from opaca.policy.client_order_id import deterministic_client_order_id
    w = world(tmp_path, qty="100", cash="100000")
    proposal, prices, bindings = _auto_sell(w, "det1")
    assert proposal.legs[0].client_order_id == deterministic_client_order_id("det1", 0)
    mutate = w.mutate()
    execute_reserved_proposal(w.store, w.read(), mutate, proposal, now=DEFAULT_NOW,
                              prices=prices, price_bindings=bindings)
    ids = {r.client_order_id for r in w.store.list_execution_orders(proposal_id="det1")}
    assert ids == {deterministic_client_order_id("det1", 0)}
    w.close()


def test_lost_ack_holds_unknown_and_never_resubmits(tmp_path):
    w = world(tmp_path, qty="100", cash="100000")
    proposal, prices, bindings = _auto_sell(w, "unk1")
    mutate = w.mutate(timeout_after_accept=True)
    result = execute_reserved_proposal(w.store, w.read(), mutate, proposal,
                                       now=DEFAULT_NOW, prices=prices,
                                       price_bindings=bindings)
    assert result.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
    assert mutate.submit_calls == 1
    for _ in range(3):
        recover_proposal(w.store, w.read(), "unk1", now=DEFAULT_NOW)
    assert mutate.submit_calls == 1
    w.close()


def test_the_final_kill_switch_still_yields_not_submitted(tmp_path):
    w = world(tmp_path, qty="100", cash="100000")
    proposal, prices, bindings = _auto_sell(w, "ks1")
    w.store.set_kill_switch(True, now=DEFAULT_NOW)
    mutate = w.mutate()
    result = execute_reserved_proposal(w.store, w.read(), mutate, proposal,
                                       now=DEFAULT_NOW, prices=prices,
                                       price_bindings=bindings)
    assert mutate.submit_calls == 0
    assert result.blocked is True
    w.close()


def test_sell_reservation_still_blocks_an_oversell(tmp_path):
    w = world(tmp_path, qty="100", cash="100000")
    first, prices, bindings = _auto_sell(w, "os1", qty="60")
    mutate = w.mutate()
    execute_reserved_proposal(w.store, w.read(), mutate, first, now=DEFAULT_NOW,
                              prices=prices, price_bindings=bindings)
    quotes = quotes_for()
    bound = bind_sell(quotes["SGOV"], Decimal("60"))
    second, prices2, bindings2 = bind_single_leg_proposal("os2", bound, quotes)
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    out = evaluate_and_reserve(w.store, second, now=DEFAULT_NOW, prices=prices2,
                              expected_snapshot_version=recon.snapshot.version,
                              price_bindings=bindings2)
    assert out.is_auto is False
    w.close()


def test_a_buy_reservation_uses_the_limit_and_releases_on_fill(tmp_path):
    from opaca.persistence.types import ReservationKind, ReservationStatus
    w = world(tmp_path, qty="0", cash="100000")
    quotes = quotes_for()
    bound = bind_buy(quotes["SGOV"], Decimal("2"))
    proposal, prices, bindings = bind_single_leg_proposal("buyres", bound, quotes)
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    assert evaluate_and_reserve(
        w.store, proposal, now=DEFAULT_NOW, prices=prices,
        expected_snapshot_version=recon.snapshot.version,
        price_bindings=bindings).is_auto is True
    active = [r for r in w.store.active_reservations()
              if r.kind is ReservationKind.CASH_DEPLOYMENT
              and r.status is ReservationStatus.ACTIVE]
    assert len(active) == 1 and active[0].amount == Decimal("2") * bound.limit_price
    mutate = w.mutate()
    execute_reserved_proposal(w.store, w.read(), mutate, proposal, now=DEFAULT_NOW,
                              prices=prices, price_bindings=bindings)
    assert mutate.submit_calls == 1
    remaining = [r for r in w.store.active_reservations()
                 if r.kind is ReservationKind.CASH_DEPLOYMENT
                 and r.status is ReservationStatus.ACTIVE]
    assert remaining == []
    w.close()
