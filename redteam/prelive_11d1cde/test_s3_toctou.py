"""S3: price TOCTOU. Is freshness still valid at the final mutation boundary?"""
from __future__ import annotations

import ast
import os
import pathlib
import time
from datetime import timedelta
from decimal import Decimal

import pytest
from opaca.execution.service import execute_reserved_proposal
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.store import SQLiteStore
from opaca.reconciliation.service import reconcile

from support import DEFAULT_NOW, bind_buy, bind_single_leg_proposal, quotes_for, world

PKG = pathlib.Path(os.environ["OPACA_BACKEND"]) / "opaca"


def _reserve(w, now=DEFAULT_NOW, age_seconds=1, qty="1"):
    quotes = quotes_for(now=now, age_seconds=age_seconds)
    bound = bind_buy(quotes["SGOV"], Decimal(qty))
    proposal, prices, bindings = bind_single_leg_proposal("toctou-1", bound, quotes)
    recon = reconcile(w.store, w.read(), now=now)
    out = evaluate_and_reserve(
        w.store, proposal, now=now, prices=prices,
        expected_snapshot_version=recon.snapshot.version, price_bindings=bindings)
    return proposal, prices, bindings, out


# ------------------------------------------------- stale before reserve


def test_a_stale_quote_is_refused_at_reservation(tmp_path):
    w = world(tmp_path, qty="0", cash="100000")
    _, _, _, out = _reserve(w, age_seconds=60)
    assert out.is_auto is False
    assert "exceeds max" in (out.block_reason or "")
    assert w.store.active_reservations() == () or all(
        r.status.value != "ACTIVE" for r in w.store.active_reservations())
    w.close()


# ------------------------------------------- fresh at reserve, stale at execute


def test_a_quote_that_goes_stale_between_reserve_and_execute_blocks_the_submit(tmp_path):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = _reserve(w, age_seconds=1)
    assert out.is_auto is True

    later = DEFAULT_NOW + timedelta(seconds=30)
    mutate = w.mutate()
    result = execute_reserved_proposal(
        w.store, w.read(), mutate, proposal, now=later, prices=prices,
        price_bindings=bindings)
    assert mutate.submit_calls == 0, "broker was mutated with a stale quote"
    assert result.blocked is True
    assert "exceeds max" in (result.block_reason or "")
    w.close()


def test_a_future_quote_at_execute_blocks_the_submit(tmp_path):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = _reserve(w, age_seconds=1)
    assert out.is_auto is True
    earlier = DEFAULT_NOW - timedelta(seconds=30)
    mutate = w.mutate()
    result = execute_reserved_proposal(
        w.store, w.read(), mutate, proposal, now=earlier, prices=prices,
        price_bindings=bindings)
    assert mutate.submit_calls == 0
    assert result.blocked is True
    w.close()


# -------------------------------------------------- kill switch in the window


def test_kill_switch_flipped_after_reservation_stops_the_submit(tmp_path):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = _reserve(w)
    assert out.is_auto is True
    w.store.set_kill_switch(True, now=DEFAULT_NOW)
    mutate = w.mutate()
    result = execute_reserved_proposal(
        w.store, w.read(), mutate, proposal, now=DEFAULT_NOW, prices=prices,
        price_bindings=bindings)
    assert mutate.submit_calls == 0
    assert result.blocked is True
    w.close()


def test_kill_switch_flipped_inside_the_final_pre_submit_window_stops_the_submit(
        tmp_path, monkeypatch):
    """Flip the switch during the last read before submit_order."""
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = _reserve(w)
    assert out.is_auto is True

    original = SQLiteStore.kill_switch_active
    state = {"n": 0}

    def instrumented(self, conn=None):
        if conn is None:
            # the final pre-submit read; an operator flips the switch right now
            state["n"] += 1
            self.set_kill_switch(True, now=DEFAULT_NOW)
        return original(self, conn)

    monkeypatch.setattr(SQLiteStore, "kill_switch_active", instrumented)
    mutate = w.mutate()
    result = execute_reserved_proposal(
        w.store, w.read(), mutate, proposal, now=DEFAULT_NOW, prices=prices,
        price_bindings=bindings)
    assert state["n"] >= 1, "no conn-less kill-switch read happened before submit"
    assert mutate.submit_calls == 0
    assert result.blocked is True
    w.close()


# ------------------------------------------ THE FINAL MUTATION BOUNDARY ITSELF


def test_FINDING_no_freshness_revalidation_between_intent_and_submit_order(
        tmp_path, monkeypatch):
    """Real elapsed time is injected in the last pre-submit window.

    The quote is fresh when the intent transaction validates it. 16 seconds of
    real time then pass before submit_order - more than the 15s policy. The
    brief requires broker submit count == 0.
    """
    import opaca.execution.service as service

    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = _reserve(w, age_seconds=1)
    assert out.is_auto is True

    validations = []
    real_validate = service.validate_canonical_quote

    def spy(q, *, now, **kw):
        validations.append(time.monotonic())
        return real_validate(q, now=now, **kw)

    monkeypatch.setattr(service, "validate_canonical_quote", spy)

    original = SQLiteStore.kill_switch_active
    delayed = {"done": False}

    def instrumented(self, conn=None):
        if conn is None and not delayed["done"]:
            delayed["done"] = True
            time.sleep(16.0)          # wall-clock drift inside the submit window
        return original(self, conn)

    monkeypatch.setattr(SQLiteStore, "kill_switch_active", instrumented)

    submits = []

    class Recording(type(w.mutate())):
        def submit_order(self, request):
            submits.append((time.monotonic(), request))
            return super().submit_order(request)

    mutate = Recording(orders=w.orders, linked_account=w.account,
                       linked_positions=w.positions, linked_open_orders=w.open_orders,
                       fill_price=Decimal("100.69"))
    execute_reserved_proposal(
        w.store, w.read(), mutate, proposal, now=DEFAULT_NOW, prices=prices,
        price_bindings=bindings)
    w.close()

    assert validations, "the intent transaction never validated the quote"
    assert delayed["done"] is True
    gap = submits[0][0] - validations[-1] if submits else None
    assert mutate.submit_calls == 0, (
        "FINDING: the broker was mutated "
        f"{gap:.1f}s after the last quote-freshness validation, exceeding the "
        f"15s policy. There is no freshness re-check at the final mutation "
        f"boundary - _submit_leg reads only the kill switch."
    )


def test_the_submit_boundary_reads_the_kill_switch_but_not_the_clock():
    """Structural: _submit_leg contains no clock read and no quote validation."""
    tree = ast.parse((PKG / "execution" / "service.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_submit_leg")
    called = {n.func.attr for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    called |= {n.func.id for n in ast.walk(fn)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "kill_switch_active" in called
    assert "validate_canonical_quote" not in called
    assert "now" not in called


def test_the_whole_execution_path_never_reads_a_wall_clock():
    """`now` is a caller parameter; the service never calls datetime.now()."""
    tree = ast.parse((PKG / "execution" / "service.py").read_text(encoding="utf-8"))
    clock_reads = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"now", "utcnow", "today"}
    ]
    assert clock_reads == []


def test_FINDING_the_live_smoke_freezes_now_before_the_quote_fetch():
    """The documented live procedure captures one `now` and reuses it for the
    quote check, the reservation and the execution."""
    src = (pathlib.Path(os.environ["OPACA_BACKEND"]) / "tests"
           / "test_live_paper_mutation.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name.startswith("test_live"))
    body = fn.body
    now_index = next(i for i, n in enumerate(body)
                     if isinstance(n, ast.Assign)
                     and any(getattr(t, "id", None) == "now" for t in n.targets))
    fetch_index = next(i for i, n in enumerate(body)
                       if isinstance(n, ast.Assign) and "latest_trades" in ast.unparse(n))
    assert now_index < fetch_index, "probe assumption: now is captured before the fetch"
    uses = [i for i, n in enumerate(body) if "now=now" in ast.unparse(n)]
    pytest.fail(
        "TOCTOU (live procedure): `now` is captured at statement "
        f"{now_index}, before the quote fetch at statement {fetch_index}, and then "
        f"reused at statements {uses} for validate_canonical_quote, "
        "evaluate_and_reserve and execute_reserved_proposal. Every freshness "
        "check therefore measures the quote against an instant that precedes "
        "the fetch, not against the instant of the broker submit. The real "
        "elapsed time across reconcile + reserve + a second reconcile + submit "
        "is unbounded and unmeasured."
    )
