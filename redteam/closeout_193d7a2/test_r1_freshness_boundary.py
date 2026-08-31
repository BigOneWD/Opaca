"""P1-1 retest: quote freshness at the FINAL mutation boundary.

The previous review's finding: `_submit_leg` read only the kill switch, so the
elapsed wall time between the last freshness validation and `submit_order` was
unmeasured. These probes assert the NEW safety property semantically: a real
clock is read immediately before `submit_order` and the exact bound canonical
quote is revalidated against it.
"""
from __future__ import annotations

import ast
import os
import pathlib
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from opaca.execution.service import execute_reserved_proposal
from opaca.persistence.store import SQLiteStore

from closeout_support import BoundaryClock, DEFAULT_NOW, buy_setup, order_states, world

PKG = pathlib.Path(os.environ["OPACA_BACKEND"]) / "opaca"
SERVICE = PKG / "execution" / "service.py"


def _run(w, proposal, prices, bindings, now=DEFAULT_NOW, mutate=None):
    mutate = mutate if mutate is not None else w.mutate()
    result = execute_reserved_proposal(
        w.store, w.read(), mutate, proposal, now=now, prices=prices,
        price_bindings=bindings)
    return mutate, result


# --------------------------------------------------------------- structural

def test_submit_leg_now_reads_a_clock_and_revalidates_the_quote():
    """Replaces the previous review's inverted structural assertions.

    Those asserted `_submit_leg` contained no clock read and no quote
    validation. That was the defect; the assertion is inverted here.
    """
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_submit_leg")
    names = {n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    attrs = {n.func.attr for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "kill_switch_active" in attrs
    assert "validate_canonical_quote" in names, "no freshness revalidation at the boundary"
    assert "_utc_now" in names, "no wall-clock read at the boundary"


def test_the_execution_service_now_reads_a_real_utc_wall_clock():
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_utc_now")
    src = ast.unparse(fn)
    assert "datetime.now(UTC)" in src.replace(" ", ""), src
    import opaca.execution.service as service
    delta = abs((service._utc_now() - datetime.now(UTC)).total_seconds())
    assert delta < 2.0
    assert service._utc_now().tzinfo is not None


def test_the_boundary_clock_is_read_after_the_intent_transaction(tmp_path, monkeypatch):
    """The boundary clock must be a *later* read than the intent validation."""
    import opaca.execution.service as service
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w)
    assert out.is_auto is True
    order: list[str] = []
    real_validate = service.validate_canonical_quote

    def spy(q, *, now, **kw):
        order.append("validate")
        return real_validate(q, now=now, **kw)

    monkeypatch.setattr(service, "validate_canonical_quote", spy)
    clock = BoundaryClock(value=DEFAULT_NOW, on_read=lambda: order.append("clock"))
    clock.install(monkeypatch)
    mutate, result = _run(w, proposal, prices, bindings)
    assert mutate.submit_calls == 1
    assert "clock" in order, "the boundary never read a clock"
    assert order.index("clock") > 0, "the clock read preceded every validation"
    assert order[order.index("clock") + 1] == "validate", (
        "the clock read is not immediately followed by a quote revalidation")
    w.close()


# ------------------------------------------------------- boundary arithmetic

@pytest.mark.parametrize(
    "delta,expected_submits,label",
    [
        (timedelta(seconds=14, microseconds=999000), 1, "14.999s"),
        (timedelta(seconds=15), 1, "15.000s (documented inclusive maximum)"),
        (timedelta(seconds=15, microseconds=1), 0, "15s + 1 microsecond"),
        (timedelta(seconds=16), 0, "16s"),
        (timedelta(seconds=-1), 0, "future quote"),
    ],
)
def test_the_exact_boundary_policy_is_enforced_at_the_mutation_boundary(
        tmp_path, monkeypatch, delta, expected_submits, label):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w)
    assert out.is_auto is True
    quote = bindings["SGOV"].quote
    clock = BoundaryClock(value=quote.source_timestamp + delta).install(monkeypatch)
    mutate, result = _run(w, proposal, prices, bindings)
    assert clock.reads, "the boundary clock was never consulted"
    assert mutate.submit_calls == expected_submits, (
        f"{label}: expected {expected_submits} submits, got {mutate.submit_calls}")
    if expected_submits == 0:
        assert result.blocked is True
        assert order_states(w.store, proposal.proposal_id) == ["NOT_SUBMITTED"]
    w.close()


def test_the_frozen_caller_now_can_no_longer_authorise_a_stale_submit(tmp_path, monkeypatch):
    """The caller's `now` says fresh; the real boundary clock says stale."""
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w)
    assert out.is_auto is True
    clock = BoundaryClock(value=DEFAULT_NOW + timedelta(seconds=45)).install(monkeypatch)
    mutate, result = _run(w, proposal, prices, bindings, now=DEFAULT_NOW)
    assert clock.reads
    assert mutate.submit_calls == 0
    assert result.blocked is True
    assert "exceeds max" in (result.block_reason or ""), result.block_reason
    w.close()


# --------------------------------------------------- real injected elapsed time

def test_sixteen_seconds_of_real_elapsed_time_before_submit_blocks(tmp_path, monkeypatch):
    """The previous review's exact reproduction, with a real clock in place.

    Control run: no injected delay -> the same setup submits. Attack run: 16
    real seconds pass inside the last pre-submit window -> zero submits.
    """
    import opaca.execution.service as service

    # control
    (tmp_path / "control").mkdir(exist_ok=True)
    w = world(tmp_path / "control", qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w, pid="delay-control")
    assert out.is_auto is True
    ctl_clock = BoundaryClock(offset_real_from=DEFAULT_NOW).install(monkeypatch)
    mutate, _ = _run(w, proposal, prices, bindings)
    assert ctl_clock.reads
    assert mutate.submit_calls == 1, "control run must submit"
    w.close()

    # attack
    (tmp_path / "attack").mkdir(exist_ok=True)
    w2 = world(tmp_path / "attack", qty="0", cash="100000")
    proposal2, prices2, bindings2, out2 = buy_setup(w2, pid="delay-attack")
    assert out2.is_auto is True

    validations: list[float] = []
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
            time.sleep(16.0)
        return original(self, conn)

    monkeypatch.setattr(SQLiteStore, "kill_switch_active", instrumented)
    atk_clock = BoundaryClock(offset_real_from=DEFAULT_NOW).install(monkeypatch)
    submits: list[float] = []

    class Recording(type(w2.mutate())):
        def submit_order(self, request):
            submits.append(time.monotonic())
            return super().submit_order(request)

    mutate2 = Recording(orders=w2.orders, linked_account=w2.account,
                        linked_positions=w2.positions,
                        linked_open_orders=w2.open_orders,
                        fill_price=Decimal("100.69"))
    result2 = execute_reserved_proposal(
        w2.store, w2.read(), mutate2, proposal2, now=DEFAULT_NOW, prices=prices2,
        price_bindings=bindings2)
    assert delayed["done"] is True, "the delay was never injected"
    assert validations, "the intent transaction never validated the quote"
    assert atk_clock.reads, "the boundary never read the clock after the delay"
    assert submits == [], "the broker was mutated after 16s of real drift"
    assert mutate2.submit_calls == 0
    assert result2.blocked is True
    assert "exceeds max" in (result2.block_reason or ""), result2.block_reason
    assert order_states(w2.store, proposal2.proposal_id) == ["NOT_SUBMITTED"]
    w2.close()


def test_an_unpatched_run_measures_genuine_wall_time_and_fails_closed(tmp_path):
    """No clock patch at all: DEFAULT_NOW is not the real instant, so the
    canonical quote cannot be fresh at the true mutation boundary."""
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w)
    assert out.is_auto is True
    mutate, result = _run(w, proposal, prices, bindings)
    assert mutate.submit_calls == 0, (
        "the boundary accepted a quote that is not fresh in real wall time")
    assert result.blocked is True
    assert order_states(w.store, proposal.proposal_id) == ["NOT_SUBMITTED"]
    w.close()


# ------------------------------------------------- kill switch in the same window

def test_kill_switch_flipped_inside_the_final_boundary_window_blocks(tmp_path, monkeypatch):
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w)
    assert out.is_auto is True
    state = {"flipped": False}

    def flip():
        if not state["flipped"]:
            state["flipped"] = True
            w.store.set_kill_switch(True, now=DEFAULT_NOW)

    clock = BoundaryClock(value=DEFAULT_NOW, on_read=flip).install(monkeypatch)
    mutate, result = _run(w, proposal, prices, bindings)
    assert state["flipped"] is True
    assert clock.reads
    assert mutate.submit_calls == 0
    assert result.blocked is True
    assert "kill switch" in (result.block_reason or ""), result.block_reason
    w.close()


# ------------------------------------------------ failure classification

def test_a_pre_mutation_failure_is_not_submitted_never_unknown(tmp_path, monkeypatch):
    from opaca.persistence.types import ExecutionState
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w)
    assert out.is_auto is True
    BoundaryClock(value=DEFAULT_NOW + timedelta(seconds=600)).install(monkeypatch)
    mutate, result = _run(w, proposal, prices, bindings)
    assert mutate.submit_calls == 0
    assert result.state is ExecutionState.NOT_SUBMITTED
    assert result.state is not ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
    assert result.submitted is False
    assert result.blocked is True
    assert (result.block_reason or "").strip() != ""
    rows = w.store.list_execution_orders(proposal_id=proposal.proposal_id)
    assert [r.state.value for r in rows] == ["NOT_SUBMITTED"]
    assert w.store.load_unknown_orders() == ()
    w.close()


def test_the_exact_bound_quote_is_the_one_revalidated(tmp_path, monkeypatch):
    """Swap a stale quote into the bindings map after the intent transaction."""
    from closeout_support import quotes_for
    from opaca.market.binding import bind_buy
    w = world(tmp_path, qty="0", cash="100000")
    proposal, prices, bindings, out = buy_setup(w)
    assert out.is_auto is True
    stale_quotes = quotes_for(now=DEFAULT_NOW, age_seconds=600)
    stale_bound = bind_buy(stale_quotes["SGOV"], Decimal("3"))

    def swap():
        bindings["SGOV"] = stale_bound

    BoundaryClock(value=DEFAULT_NOW, on_read=lambda: None).install(monkeypatch)
    original = SQLiteStore.kill_switch_active
    done = {"x": False}

    def instrumented(self, conn=None):
        if conn is None and not done["x"]:
            done["x"] = True
            swap()
        return original(self, conn)

    monkeypatch.setattr(SQLiteStore, "kill_switch_active", instrumented)
    mutate, result = _run(w, proposal, prices, bindings)
    assert done["x"] is True
    assert mutate.submit_calls == 0, "a stale quote swapped in before submit reached the broker"
    assert result.blocked is True
    w.close()
