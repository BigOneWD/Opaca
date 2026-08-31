"""Critical-regression spot-checks under the new mandatory-binding contract.

Every probe drives the REAL AlpacaPaperExecutionGateway class unless it needs a
fault-injecting double, in which case it uses the production offline double.
"""
from __future__ import annotations

import ast
import os
import pathlib
import threading
from decimal import Decimal

import pytest
from opaca.broker.errors import BrokerUnavailableError, PaperEnvironmentError
from opaca.execution.gateway import FakePaperExecutionGateway
from opaca.execution.service import execute_reserved_proposal, recover_proposal
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.store import SQLiteStore
from opaca.persistence.types import ExecutionState
from opaca.reconciliation.service import reconcile
from opaca.domain.models import Side

from closeout_support import (
    BoundaryClock,
    DEFAULT_NOW,
    buy_setup,
    quotes_for,
    real_paper_gateway,
    world,
)
from closeout_support import leg, proposal_of
from opaca.market.binding import bind_sell

PKG = pathlib.Path(os.environ["OPACA_BACKEND"]) / "opaca"


def _sell_setup(w, pid, qty="60", now=DEFAULT_NOW):
    quotes = quotes_for(now=now)
    bound = bind_sell(quotes["SGOV"], Decimal(qty))
    legs = [leg(pid, 0, "SGOV", Side.SELL, qty, bound.reference_price)]
    proposal = proposal_of(pid, legs)
    prices = {s: q.price for s, q in quotes.items()}
    recon = reconcile(w.store, w.read(), now=now)
    out = evaluate_and_reserve(
        w.store, proposal, now=now, prices=prices,
        expected_snapshot_version=recon.snapshot.version,
        price_bindings={"SGOV": bound})
    return proposal, prices, {"SGOV": bound}, out


# ------------------------------------------------------------ duplicate safety

def test_duplicate_order_prevention_survives_five_retries(tmp_path, monkeypatch):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w, pid="dup-1")
    assert out.is_auto is True
    gateway, client = real_paper_gateway()
    BoundaryClock(value=DEFAULT_NOW).install(monkeypatch)
    for _ in range(5):
        execute_reserved_proposal(
            w.store, w.read(), gateway, proposal, now=DEFAULT_NOW, prices=prices,
            price_bindings=bindings)
    assert client.submit_calls == 1
    rows = w.store.list_execution_orders(proposal_id=proposal.proposal_id)
    assert len(rows) == 1
    w.close()


def test_concurrent_executors_submit_once(tmp_path, monkeypatch):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w, pid="dup-2")
    assert out.is_auto is True
    gateway, client = real_paper_gateway()
    BoundaryClock(value=DEFAULT_NOW).install(monkeypatch)
    errors: list[BaseException] = []

    def run():
        store = SQLiteStore(w.store.path)
        try:
            execute_reserved_proposal(
                store, w.read(), gateway, proposal, now=DEFAULT_NOW, prices=prices,
                price_bindings=bindings)
        except Exception as exc:  # recorded, not asserted: contention is allowed
            errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert client.submit_calls == 1, errors
    assert len(w.store.list_execution_orders(proposal_id=proposal.proposal_id)) == 1
    w.close()


def test_the_client_order_id_is_deterministic_and_correlates(tmp_path, monkeypatch):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w, pid="corr-1")
    gateway, client = real_paper_gateway()
    BoundaryClock(value=DEFAULT_NOW).install(monkeypatch)
    execute_reserved_proposal(
        w.store, w.read(), gateway, proposal, now=DEFAULT_NOW, prices=prices,
        price_bindings=bindings)
    cid = proposal.legs[0].client_order_id
    assert cid.startswith("opaca-")
    assert client.orders[cid]["client_order_id"] == cid
    rows = w.store.list_execution_orders(proposal_id=proposal.proposal_id)
    assert rows[0].client_order_id == cid
    reservations = w.store.active_reservations()
    assert all(r.proposal_id == proposal.proposal_id for r in reservations)
    w.close()


# ------------------------------------------------------------- UNKNOWN recovery

def test_lost_ack_becomes_unknown_and_never_resubmits(tmp_path, monkeypatch):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w, pid="unk-1")
    assert out.is_auto is True
    BoundaryClock(value=DEFAULT_NOW).install(monkeypatch)
    mutate = w.mutate(timeout_before_accept=True)
    result = execute_reserved_proposal(
        w.store, w.read(), mutate, proposal, now=DEFAULT_NOW, prices=prices,
        price_bindings=bindings)
    assert result.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
    assert mutate.submit_calls == 1
    for _ in range(3):
        again = recover_proposal(w.store, w.read(), proposal.proposal_id, now=DEFAULT_NOW)
        assert again.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
        assert again.recovered is True
    assert mutate.submit_calls == 1
    w.close()


# ---------------------------------------------------------------- kill switch

def test_the_final_kill_switch_still_stops_everything(tmp_path, monkeypatch):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w, pid="kill-1")
    w.store.set_kill_switch(True, now=DEFAULT_NOW)
    gateway, client = real_paper_gateway()
    BoundaryClock(value=DEFAULT_NOW).install(monkeypatch)
    result = execute_reserved_proposal(
        w.store, w.read(), gateway, proposal, now=DEFAULT_NOW, prices=prices,
        price_bindings=bindings)
    assert client.submit_calls == 0
    assert result.blocked is True
    w.close()


# ------------------------------------------------------ reservations & exposure

def test_a_buy_reserves_quantity_times_the_bounded_limit(tmp_path):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w, pid="res-buy", qty="3")
    assert out.is_auto is True
    limit = bindings["SGOV"].limit_price
    assert limit == Decimal("100.80")
    cash_rows = [r for r in w.store.active_reservations()
                 if r.amount is not None and r.kind.value.upper().startswith("CASH")]
    assert cash_rows, [r.kind.value for r in w.store.active_reservations()]
    reserved = sum(r.amount for r in cash_rows)
    assert reserved == Decimal("3") * limit
    assert reserved > Decimal("3") * Decimal("100.69")
    assert proposal.legs[0].notional == Decimal("3") * limit
    w.close()


def test_a_sell_reservation_still_blocks_an_oversell(tmp_path, monkeypatch):
    w = world(tmp_path, qty="100", cash="100000")
    proposal, prices, bindings, out = _sell_setup(w, "sell-1", qty="60")
    assert out.is_auto is True
    BoundaryClock(value=DEFAULT_NOW).install(monkeypatch)
    mutate = w.mutate()
    execute_reserved_proposal(
        w.store, w.read(), mutate, proposal, now=DEFAULT_NOW, prices=prices,
        price_bindings=bindings)
    assert mutate.submit_calls == 1
    p2, pr2, b2, out2 = _sell_setup(w, "sell-2", qty="60")
    assert out2.is_auto is False, "a second 60-share SELL of a 100-share position was allowed"
    w.close()


def test_partial_fills_are_tracked(tmp_path, monkeypatch):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w, pid="part-1", qty="4")
    BoundaryClock(value=DEFAULT_NOW).install(monkeypatch)
    mutate = w.mutate(partial_fill_qty=Decimal("1"))
    result = execute_reserved_proposal(
        w.store, w.read(), mutate, proposal, now=DEFAULT_NOW, prices=prices,
        price_bindings=bindings)
    assert mutate.submit_calls == 1
    assert result.filled_quantity == Decimal("1")
    assert result.remaining_quantity == Decimal("3")
    assert result.state is ExecutionState.PARTIALLY_FILLED
    w.close()


# ---------------------------------------------------------- bounded DAY LIMIT

def test_the_submitted_order_is_a_bounded_day_limit(tmp_path, monkeypatch):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w, pid="limit-1", qty="3")
    gateway, client = real_paper_gateway()
    BoundaryClock(value=DEFAULT_NOW).install(monkeypatch)
    execute_reserved_proposal(
        w.store, w.read(), gateway, proposal, now=DEFAULT_NOW, prices=prices,
        price_bindings=bindings)
    order = client.seen[0]
    assert getattr(order, "type").value == "limit"
    assert getattr(order, "time_in_force").value == "day"
    assert Decimal(str(getattr(order, "limit_price"))) == Decimal("100.80")
    assert Decimal(str(getattr(order, "limit_price"))) > Decimal("100.69")
    w.close()


# --------------------------------------------------------- paper-only surface

def test_the_mutation_surface_is_still_exactly_two_call_sites():
    sites = []
    for path in PKG.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"submit_order", "cancel_order_by_id"}
                    and ast.unparse(node.func.value) == "mutate_gateway"):
                sites.append(f"{path.name}:{node.lineno}")
    assert sorted(sites) == sorted(["service.py:328", "service.py:607"]), sites


def test_zero_bare_asserts_in_production():
    offenders = []
    for path in PKG.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [f"{path.name}:{n.lineno}" for n in ast.walk(tree)
                      if isinstance(n, ast.Assert)]
    assert offenders == [], offenders


def test_a_live_endpoint_gateway_cannot_mutate(tmp_path, monkeypatch):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w, pid="live-1")
    live = FakePaperExecutionGateway(endpoint="https://api.alpaca.markets")
    BoundaryClock(value=DEFAULT_NOW).install(monkeypatch)
    with pytest.raises(PaperEnvironmentError):
        execute_reserved_proposal(
            w.store, w.read(), live, proposal, now=DEFAULT_NOW, prices=prices,
            price_bindings=bindings)
    assert live.submit_calls == 0
    w.close()


# ------------------------------------------------------------ read-only preflight

def test_the_preflight_module_still_names_no_mutator():
    src = (PKG / "preflight.py").read_text(encoding="utf-8")
    for forbidden in ("submit_order", "cancel_order_by_id", "evaluate_and_reserve",
                      "execute_reserved_proposal", "grant_human_approval",
                      "PaperMutatingGateway", "open_paper_execution_gateway_from_env"):
        assert forbidden not in src, forbidden


def test_the_preflight_refuses_a_look_alike_endpoint():
    from opaca.preflight import _verify_paper_endpoint

    class _G:
        endpoint = "https://paper-api.alpaca.markets.evil.com"

    with pytest.raises(PaperEnvironmentError):
        _verify_paper_endpoint(_G())
