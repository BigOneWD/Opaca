"""P1-2 retest: canonical binding as a PRECONDITION of real broker mutation.

Every probe here drives the REAL production class
``opaca.broker.paper_execution.AlpacaPaperExecutionGateway`` (constructed over a
shape-compatible stub client, so every production guard runs). Nothing leaves
the process; no credentials are read.
"""
from __future__ import annotations

import ast
import os
import pathlib
from datetime import timedelta
from decimal import Decimal

import pytest
from opaca.domain.models import Proposal, Side
from opaca.execution.service import execute_reserved_proposal
from opaca.market.binding import bind_buy, bind_sell, price_binding_failure
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.reconciliation.service import reconcile

from closeout_support import (
    BoundaryClock,
    DEFAULT_NOW,
    buy_setup,
    order_states,
    quotes_for,
    real_paper_gateway,
    world,
)
from closeout_support import leg, proposal_of

PKG = pathlib.Path(os.environ["OPACA_BACKEND"]) / "opaca"


def _execute_real(w, proposal, prices, bindings, monkeypatch, now=DEFAULT_NOW,
                  boundary=None):
    gateway, client = real_paper_gateway()
    BoundaryClock(value=boundary or now).install(monkeypatch)
    result = execute_reserved_proposal(
        w.store, w.read(), gateway, proposal, now=now, prices=prices,
        price_bindings=bindings)
    return client, result


# ------------------------------------------------------- the real gateway itself

def test_the_probe_really_drives_the_production_paper_gateway_class():
    from opaca.broker.paper_execution import AlpacaPaperExecutionGateway
    gateway, client = real_paper_gateway()
    assert isinstance(gateway, AlpacaPaperExecutionGateway)
    assert type(gateway).__module__ == "opaca.broker.paper_execution"
    assert gateway.endpoint == "https://paper-api.alpaca.markets"
    assert client.submit_calls == 0


def test_a_valid_canonical_binding_may_proceed(tmp_path, monkeypatch):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w, pid="bind-ok")
    assert out.is_auto is True
    client, result = _execute_real(w, proposal, prices, bindings, monkeypatch)
    assert client.submit_calls == 1, "a fully bound canonical proposal must be able to proceed"
    order = client.seen[0]
    limit = getattr(order, "limit_price", None)
    assert Decimal(str(limit)) == Decimal("100.80"), limit
    assert Decimal(str(getattr(order, "qty", None))) == Decimal("3")
    assert getattr(order, "time_in_force", None).value == "day"
    assert getattr(order, "type", None).value == "limit"
    assert getattr(order, "side", None).value == "buy"
    # observation, recorded not asserted: alpaca-py coerces the exact decimal
    # string to a binary float at the SDK boundary.
    print(f"[observation] SDK limit_price type={type(limit).__name__} value={limit!r}")
    w.close()


# ------------------------------------------------------------ missing bindings

@pytest.mark.parametrize("mode", ["none", "empty"])
def test_missing_or_empty_bindings_cannot_reach_the_real_broker(tmp_path, monkeypatch, mode):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w, pid=f"bind-{mode}")
    assert out.is_auto is True
    supplied = None if mode == "none" else {}
    client, result = _execute_real(w, proposal, prices, supplied, monkeypatch)
    assert client.submit_calls == 0
    assert result.blocked is True
    assert "bindings are" in (result.block_reason or ""), result.block_reason
    assert order_states(w.store, proposal.proposal_id) == []
    w.close()


def test_incomplete_bindings_on_a_multi_leg_proposal_cannot_reach_the_broker(
        tmp_path, monkeypatch):
    w = world(tmp_path, qty="0", cash="100000")
    quotes = quotes_for(now=DEFAULT_NOW)
    b_sgov = bind_buy(quotes["SGOV"], Decimal("2"))
    b_bil = bind_buy(quotes["BIL"], Decimal("2"))
    legs = [
        leg("multi-1", 0, "SGOV", Side.BUY, "2", b_sgov.reference_price),
        leg("multi-1", 1, "BIL", Side.BUY, "2", b_bil.reference_price),
    ]
    proposal = proposal_of("multi-1", legs)
    prices = {s: q.price for s, q in quotes.items()}
    full = {"SGOV": b_sgov, "BIL": b_bil}
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    out = evaluate_and_reserve(
        w.store, proposal, now=DEFAULT_NOW, prices=prices,
        expected_snapshot_version=recon.snapshot.version, price_bindings=full)
    assert out.is_auto is True
    partial = {"SGOV": b_sgov}
    client, result = _execute_real(w, proposal, prices, partial, monkeypatch)
    assert client.submit_calls == 0
    assert result.blocked is True
    w.close()


# --------------------------------------------------------------- wrong bindings

def test_a_binding_for_the_wrong_symbol_cannot_reach_the_broker(tmp_path, monkeypatch):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w, pid="bind-wrongsym")
    assert out.is_auto is True
    quotes = quotes_for(now=DEFAULT_NOW)
    wrong = {"BIL": bind_buy(quotes["BIL"], Decimal("3"))}
    client, result = _execute_real(w, proposal, prices, wrong, monkeypatch)
    assert client.submit_calls == 0
    assert result.blocked is True
    w.close()


def test_a_binding_with_the_wrong_quantity_cannot_reach_the_broker(tmp_path, monkeypatch):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w, pid="bind-wrongqty", qty="3")
    assert out.is_auto is True
    quotes = quotes_for(now=DEFAULT_NOW)
    wrong = {"SGOV": bind_buy(quotes["SGOV"], Decimal("300"))}
    client, result = _execute_real(w, proposal, prices, wrong, monkeypatch)
    assert client.submit_calls == 0
    assert result.blocked is True
    w.close()


def test_a_mismatched_canonical_quote_cannot_reach_the_broker(tmp_path, monkeypatch):
    """The binding is internally valid but prices a different print."""
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w, pid="bind-mismatch", qty="3")
    assert out.is_auto is True
    cheap = quotes_for(now=DEFAULT_NOW, sgov=Decimal("0.01"))
    wrong = {"SGOV": bind_buy(cheap["SGOV"], Decimal("3"))}
    assert price_binding_failure(proposal, prices, bindings=wrong) is not None
    client, result = _execute_real(w, proposal, prices, wrong, monkeypatch)
    assert client.submit_calls == 0
    assert result.blocked is True
    w.close()


# ------------------------------------------ the invented matched $0.01 caller pair

def test_an_invented_matched_one_cent_pair_cannot_reach_the_real_broker(
        tmp_path, monkeypatch):
    """The exact previous-review exploit, now against the real gateway.

    A caller invents prices['SGOV'] = 0.01 AND reference_price = 0.01. The two
    surfaces agree, so the unbound reservation guard is satisfied. The mutating
    path must still refuse: those numbers are not a canonical quote.
    """
    w = world(tmp_path, qty="0", cash="100000")
    prices = {"SGOV": Decimal("0.01")}
    legs = [leg("cent-1", 0, "SGOV", Side.BUY, "100000", "0.01")]
    proposal = proposal_of("cent-1", legs)
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    out = evaluate_and_reserve(
        w.store, proposal, now=DEFAULT_NOW, prices=prices,
        expected_snapshot_version=recon.snapshot.version)
    # The offline/unbound reservation mode may still say AUTO ...
    unbound_auto = out.is_auto
    # ... but it must not be consumable as real PAPER execution authority.
    client, result = _execute_real(w, proposal, prices, None, monkeypatch)
    assert client.submit_calls == 0, (
        "an invented matched $0.01 pair reached the real paper broker")
    assert result.blocked is True
    assert "bindings are required" in (result.block_reason or ""), result.block_reason
    assert order_states(w.store, proposal.proposal_id) == []
    print(f"[observation] unbound reservation is_auto={unbound_auto}; submits=0")
    w.close()


def test_an_invented_one_cent_sell_cannot_reach_the_real_broker(tmp_path, monkeypatch):
    w = world(tmp_path, qty="100", cash="100000")
    prices = {"SGOV": Decimal("0.01")}
    legs = [leg("cent-sell", 0, "SGOV", Side.SELL, "100", "0.01")]
    proposal = proposal_of("cent-sell", legs)
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    evaluate_and_reserve(
        w.store, proposal, now=DEFAULT_NOW, prices=prices,
        expected_snapshot_version=recon.snapshot.version)
    client, result = _execute_real(w, proposal, prices, None, monkeypatch)
    assert client.submit_calls == 0, "an invented $0.01 SELL was marketable at the real broker"
    assert result.blocked is True
    w.close()


# ------------------------------- UNBOUND RESERVATION AUTO is not execution authority

def test_an_unbound_auto_reservation_is_never_consumable_as_real_execution(
        tmp_path, monkeypatch):
    """Honest prices, honest quantity, AUTO reservation taken WITHOUT bindings.

    Even this - nothing invented anywhere - must not reach the real broker,
    because execution authority may only be constructed from a canonical quote.
    """
    w = world(tmp_path, qty="0", cash="100000")
    prices = {"SGOV": Decimal("100.69")}
    legs = [leg("unbound-1", 0, "SGOV", Side.BUY, "3", "100.69")]
    proposal = proposal_of("unbound-1", legs)
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    out = evaluate_and_reserve(
        w.store, proposal, now=DEFAULT_NOW, prices=prices,
        expected_snapshot_version=recon.snapshot.version)
    assert out.is_auto is True, "probe assumption: unbound offline reservation still reaches AUTO"
    client, result = _execute_real(w, proposal, prices, None, monkeypatch)
    assert client.submit_calls == 0
    assert result.blocked is True
    assert "bindings are required" in (result.block_reason or "")
    # and it stays refused on replay
    client2, result2 = _execute_real(w, proposal, prices, None, monkeypatch)
    assert client2.submit_calls == 0
    w.close()


def test_the_fake_gateway_path_refuses_unbound_execution_identically(tmp_path, monkeypatch):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w, pid="unbound-fake")
    assert out.is_auto is True
    BoundaryClock(value=DEFAULT_NOW).install(monkeypatch)
    mutate = w.mutate()
    result = execute_reserved_proposal(
        w.store, w.read(), mutate, proposal, now=DEFAULT_NOW, prices=prices,
        price_bindings=None)
    assert mutate.submit_calls == 0
    assert result.blocked is True
    w.close()


# ------------------------------------------------------------------- structural

def test_every_route_to_submit_order_carries_bindings():
    """submit_order has exactly one call site, inside _submit_leg, and every
    caller of _submit_leg passes price_bindings."""
    tree = ast.parse((PKG / "execution" / "service.py").read_text(encoding="utf-8"))
    submit_sites = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "submit_order"
    ]
    assert len(submit_sites) == 1, [ast.unparse(n) for n in submit_sites]
    enclosing = [
        fn.name for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        and any(n is submit_sites[0] for n in ast.walk(fn))
    ]
    assert "_submit_leg" in enclosing
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_submit_leg"]
    assert calls, "no call to _submit_leg found"
    for call in calls:
        kw = {k.arg for k in call.keywords}
        assert "price_bindings" in kw, ast.unparse(call)


def test_the_intent_transaction_refuses_none_and_empty_bindings_explicitly():
    src = (PKG / "execution" / "service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_persist_submission_intents")
    body = ast.unparse(fn)
    assert "price_bindings is None" in body
    assert "if not price_bindings" in body
    assert "required" in body
