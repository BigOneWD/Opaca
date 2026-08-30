"""P1: multi-leg proposals, TOCTOU windows, and cross-proposal isolation."""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from opaca.domain.models import Side
from opaca.execution.service import (
    execute_reserved_proposal,
    recover_proposal,
)
from opaca.persistence.store import SQLiteStore
from opaca.persistence.types import ExecutionState, ReconciliationStatus
from opaca.reconciliation.service import reconcile

from p3_support import (
    DEFAULT_NOW,
    DEFAULT_PRICES,
    SGOV,
    World,
    active_sell_qty,
    make_order,
    make_proposal,
    reserve,
    sell,
    world,
)
from tests.state_helpers import account_payload, position_payload


def _execute(w, proposal, gw=None, **kw):
    return execute_reserved_proposal(
        w.store, w.read(), gw if gw is not None else w.mutate(**kw),
        proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
    )


def _two_leg_world(tmp_path):
    store = SQLiteStore(tmp_path / "opaca.sqlite")
    w = World(
        store=store,
        account=dict(account_payload(cash="100000")),
        positions=[
            dict(position_payload(qty="100")),
            {"symbol": "BIL", "side": "long", "qty": "50", "qty_available": "50",
             "market_value": "4600"},
        ],
    )
    assert reconcile(store, w.read(), now=DEFAULT_NOW).status is (
        ReconciliationStatus.RECONCILED
    )
    proposal = make_proposal("m1", [
        make_order("m1", 0, "SGOV", Side.SELL, "10", SGOV),
        make_order("m1", 1, "BIL", Side.SELL, "1", DEFAULT_PRICES["BIL"]),
    ])
    _, out = reserve(w, proposal)
    assert out.is_auto, out.authority_result
    return w, proposal


def test_m01_both_legs_submit_exactly_once(tmp_path):
    w, proposal = _two_leg_world(tmp_path)
    gw = w.mutate()
    result = _execute(w, proposal, gw)
    assert gw.submit_calls == 2
    rows = w.store.list_execution_orders(proposal_id="m1")
    assert len(rows) == 2
    assert all(r.state is ExecutionState.FILLED for r in rows)
    assert active_sell_qty(w.store, "SGOV") == Decimal("0")
    assert active_sell_qty(w.store, "BIL") == Decimal("0")
    w.close()


def test_m02_a_lost_first_leg_stops_the_second(tmp_path):
    """Uncertainty on leg 0 must not let leg 1 go out."""
    w, proposal = _two_leg_world(tmp_path)
    gw = w.mutate(timeout_before_accept=True)
    _execute(w, proposal, gw)
    assert gw.submit_calls == 1, "the second leg must not be submitted after an UNKNOWN"
    assert active_sell_qty(w.store, "SGOV") == Decimal("10")
    assert active_sell_qty(w.store, "BIL") == Decimal("1")
    w.close()


def test_m03_retrying_a_partially_submitted_proposal_never_resubmits(tmp_path):
    w, proposal = _two_leg_world(tmp_path)
    gw = w.mutate(timeout_before_accept=True)
    _execute(w, proposal, gw)
    for _ in range(3):
        again = _execute(w, proposal, gw)
        assert again.submitted is False
    assert gw.submit_calls == 1
    w.close()


def test_FINDING_a_rejected_first_leg_strands_the_remaining_legs(tmp_path):
    """Intents for every leg are committed before the first submit. If leg 0 comes
    back REJECTED the loop breaks, leaving leg 1 permanently in SUBMITTING for an
    order that was never sent - and recovery can only reclassify it as UNKNOWN."""
    w, proposal = _two_leg_world(tmp_path)
    gw = w.mutate(reject_reason="insufficient shares")
    result = _execute(w, proposal, gw)
    assert gw.submit_calls == 1, "the loop stops after the rejection"
    rows = {r.leg_index: r for r in w.store.list_execution_orders(proposal_id="m1")}
    assert rows[0].state is ExecutionState.REJECTED
    stranded = rows[1]
    assert stranded.state is ExecutionState.SUBMITTING, "probe assumption"
    assert stranded.broker_order_id is None, "leg 1 never reached the broker"
    # reservations for BOTH legs are retained because leg 1 is not terminal
    assert active_sell_qty(w.store, "SGOV") == Decimal("10")
    assert active_sell_qty(w.store, "BIL") == Decimal("1")
    # recovery cannot tell "never sent" from "lost": it becomes UNKNOWN
    recovered = recover_proposal(w.store, w.read(), "m1", now=DEFAULT_NOW)
    after = {r.leg_index: r for r in w.store.list_execution_orders(proposal_id="m1")}
    w.close()
    assert after[1].state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
    assert recovered.blocked is True
    pytest.fail(
        "FINDING P1-3: a REJECTED first leg leaves every later leg stranded in "
        "SUBMITTING for an order that was never sent. Recovery reclassifies it as "
        "UNKNOWN_REQUIRES_RECONCILIATION, which permanently retains the whole "
        "proposal's reservations and requires human review for an order that does not "
        "exist. Fail-closed, but the account jams and only manual intervention clears it."
    )


def test_m04_cross_proposal_isolation_under_concurrency(tmp_path):
    """Two distinct proposals executing at once must not interfere."""
    w = world(tmp_path, qty="100")
    first = sell("s1", "10")
    second = sell("s2", "20")
    reserve(w, first)
    reserve(w, second)
    path = w.store.path
    w.store.close()
    gw = w.mutate(fill_on_submit=False)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def run(proposal):
        try:
            local = SQLiteStore(path, timeout=20.0)
            barrier.wait(timeout=20)
            execute_reserved_proposal(
                local, w.read(), gw, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES
            )
            local.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(p,)) for p in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert gw.submit_calls == 2
    check = SQLiteStore(path)
    try:
        assert len(check.list_execution_orders()) == 2
        ids = {r.proposal_id for r in check.list_execution_orders()}
        assert ids == {"s1", "s2"}
    finally:
        check.close()


def test_m05_the_intent_transaction_rejects_a_stale_snapshot_version(tmp_path):
    """White-box: the submission-intent gate must refuse a version that moved."""
    from opaca.execution.service import _persist_submission_intents

    w = world(tmp_path)
    proposal = sell("s1", "10")
    version, _ = reserve(w, proposal)
    reason = _persist_submission_intents(
        w.store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
        calendar=__import__(
            "opaca.calendar.us_trading_calendar", fromlist=["US_TRADING_CALENDAR"]
        ).US_TRADING_CALENDAR,
        environment_verified=True,
        expected_snapshot_version=version + 99,
    )
    assert reason == "stale snapshot"
    assert w.store.list_execution_orders(proposal_id="s1") == ()
    w.close()


def test_m06_environment_not_verified_blocks_submission(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    gw = w.mutate()
    result = execute_reserved_proposal(
        w.store, w.read(), gw, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
        environment_verified=False,
    )
    assert result.blocked is True
    assert gw.submit_calls == 0
    w.close()


def test_m07_a_blocked_attempt_leaves_the_proposal_executable_later(tmp_path):
    """Blocking must not poison the proposal: clearing the cause allows execution."""
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    w.store.set_kill_switch(True, now=DEFAULT_NOW)
    gw = w.mutate()
    assert _execute(w, proposal, gw).blocked is True
    assert gw.submit_calls == 0
    w.store.set_kill_switch(False, now=DEFAULT_NOW)
    result = _execute(w, proposal, gw)
    assert result.blocked is False
    assert gw.submit_calls == 1
    w.close()


def test_FINDING_kill_switch_flipped_after_revalidation_still_submits(tmp_path):
    """The kill switch is checked inside the intent transaction, then the broker call
    happens outside it. A flip in that window is not seen."""
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    gw = w.mutate()
    original = gw.submit_order

    def flip_then_submit(request):
        w.store.set_kill_switch(True, now=DEFAULT_NOW)
        return original(request)

    gw.submit_order = flip_then_submit  # type: ignore[method-assign]
    result = _execute(w, proposal, gw)
    w.close()
    assert gw.submit_calls == 1, "probe assumption"
    assert result.state is ExecutionState.FILLED
    pytest.fail(
        "FINDING P3-1: the kill switch is evaluated inside the submission-intent "
        "transaction and is not re-checked immediately before mutate_gateway."
        "submit_order(), so a flip inside that window does not stop the order. The "
        "window is small and closing it fully is impossible against a remote broker, "
        "but a last-moment re-read costs one row."
    )
