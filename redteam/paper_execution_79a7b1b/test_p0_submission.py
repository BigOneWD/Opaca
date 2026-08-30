"""P0: at most one broker order per logical leg, ever."""

from __future__ import annotations

import threading
from datetime import timedelta
from decimal import Decimal

import pytest
from opaca.broker.errors import PaperEnvironmentError
from opaca.execution.errors import ExecutionBlockedError
from opaca.execution.gateway import FakePaperExecutionGateway
from opaca.execution.service import (
    execute_reserved_proposal,
    recover_open_executions,
    recover_proposal,
)
from opaca.persistence.store import SQLiteStore
from opaca.persistence.types import AuditEventType, ExecutionState
from opaca.reconciliation.service import reconcile

from p3_support import (
    DEFAULT_NOW,
    DEFAULT_PRICES,
    SGOV,
    active_cash,
    active_sell_qty,
    buy,
    reserve,
    sell,
    world,
)


def _execute(w, proposal, mutate=None, now=DEFAULT_NOW, **kw):
    return execute_reserved_proposal(
        w.store, w.read(), mutate if mutate is not None else w.mutate(**kw),
        proposal, now=now, prices=DEFAULT_PRICES,
    )


# ------------------------------------------------------------ one submit only


def test_s01_happy_path_submits_exactly_once(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    _, reserved = reserve(w, proposal)
    assert reserved.is_auto
    gw = w.mutate()
    result = _execute(w, proposal, gw)
    assert gw.submit_calls == 1
    assert result.submitted is True
    assert result.state is ExecutionState.FILLED
    assert result.blocked is False
    w.close()


def test_s02_exact_retry_never_submits_twice(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    gw = w.mutate()
    _execute(w, proposal, gw)
    for _ in range(5):
        again = _execute(w, proposal, gw)
        assert again.recovered is True
        assert again.submitted is False
    assert gw.submit_calls == 1
    rows = w.store.list_execution_orders(proposal_id="s1")
    assert len(rows) == 1
    w.close()


def test_s03_concurrent_executors_submit_once(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    path = w.store.path
    w.store.close()
    gw = w.mutate()
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []
    results: list[object] = []

    def worker():
        try:
            local = SQLiteStore(path, timeout=20.0)
            barrier.wait(timeout=20)
            results.append(
                execute_reserved_proposal(
                    local, w.read(), gw, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES
                )
            )
            local.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert gw.submit_calls == 1, f"{gw.submit_calls} broker submissions for one logical leg"
    check = SQLiteStore(path)
    try:
        assert len(check.list_execution_orders(proposal_id="s1")) == 1
    finally:
        check.close()


def test_s04_broker_duplicate_client_order_id_is_looked_up_not_resubmitted(tmp_path):
    """The broker already has this client_order_id: never mint a new one."""
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    coid = proposal.legs[0].client_order_id
    # the order already exists at the broker, unknown to us locally
    w.orders[coid] = {
        "id": "broker-pre", "client_order_id": coid, "symbol": "SGOV", "side": "sell",
        "status": "filled", "qty": "10", "filled_qty": "10", "filled_avg_price": "100.69",
    }
    gw = w.mutate()
    result = _execute(w, proposal, gw)
    assert gw.submit_calls == 1, "one attempt is fine; a second would be a duplicate order"
    assert result.state is ExecutionState.FILLED
    kinds = [e.event_type for e in w.store.list_audit(proposal_id="s1")]
    assert AuditEventType.ORDER_RECOVERED in kinds
    w.close()


@pytest.mark.parametrize("mode", ["timeout_before_accept", "timeout_after_accept"])
def test_s05_lost_response_becomes_unknown_and_never_resubmits(tmp_path, mode):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    gw = w.mutate(**{mode: True})
    result = _execute(w, proposal, gw)
    assert result.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
    assert gw.submit_calls == 1
    record = w.store.get_execution_order(proposal.legs[0].client_order_id)
    assert record.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
    # retrying must never mint a second order
    for _ in range(3):
        again = _execute(w, proposal, gw)
        assert again.submitted is False
    assert gw.submit_calls == 1
    assert AuditEventType.ORDER_UNKNOWN in [
        e.event_type for e in w.store.list_audit(proposal_id="s1")
    ]
    w.close()


def test_s06_unknown_retains_capacity(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    before = active_sell_qty(w.store)
    assert before == Decimal("10")
    gw = w.mutate(timeout_before_accept=True)
    result = _execute(w, proposal, gw)
    assert result.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
    assert active_sell_qty(w.store) == before, "a timeout is not a release"
    assert result.reservation_active is True
    w.close()


def test_s07_crash_between_intent_and_submit_never_resubmits(tmp_path):
    """Simulate the process dying after the intent row is committed."""
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    gw = w.mutate()

    boom = FakePaperExecutionGateway(endpoint=gw.endpoint, orders={})

    def die(request):
        raise KeyboardInterrupt("process killed between intent and broker call")

    boom.submit_order = die  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        _execute(w, proposal, boom)
    record = w.store.get_execution_order(proposal.legs[0].client_order_id)
    assert record is not None
    assert record.state is ExecutionState.SUBMITTING, "the intent must survive the crash"
    # restart recovery must look up, never resubmit
    results = recover_open_executions(w.store, w.read(), now=DEFAULT_NOW)
    assert len(results) == 1
    assert results[0].submitted is False
    assert gw.submit_calls == 0
    assert active_sell_qty(w.store) == Decimal("10"), "capacity retained under uncertainty"
    w.close()


def test_s08_recovery_after_crash_finds_a_landed_order(tmp_path):
    """Intent persisted, order actually reached the broker, we never heard back."""
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    coid = proposal.legs[0].client_order_id
    gw = w.mutate(timeout_after_accept=True)
    _execute(w, proposal, gw)
    assert gw.submit_calls == 1
    assert coid in w.orders, "probe assumption: the order landed at the broker"
    recovered = recover_proposal(w.store, w.read(), "s1", now=DEFAULT_NOW)
    assert recovered.state is ExecutionState.FILLED
    assert gw.submit_calls == 1
    assert active_sell_qty(w.store) == Decimal("0"), "proven fill releases capacity"
    w.close()


# ------------------------------------------------------------ pre-submit gates


def test_s09_kill_switch_blocks_before_any_broker_call(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    w.store.set_kill_switch(True, now=DEFAULT_NOW)
    gw = w.mutate()
    result = _execute(w, proposal, gw)
    assert result.blocked is True
    assert result.block_reason == "kill switch active"
    assert gw.submit_calls == 0, "the kill switch must stop the order before the broker"
    assert w.store.list_execution_orders(proposal_id="s1") == ()
    assert active_sell_qty(w.store) == Decimal("10")
    w.close()


def test_s10_unreserved_proposal_cannot_execute(tmp_path):
    w = world(tmp_path)
    proposal = sell("never-reserved", "10")
    gw = w.mutate()
    result = _execute(w, proposal, gw)
    assert result.blocked is True
    assert gw.submit_calls == 0
    w.close()


def test_s11_payload_mutation_after_reservation_is_blocked(tmp_path):
    w = world(tmp_path)
    reserve(w, sell("s1", "10"))
    mutated = sell("s1", "90")
    gw = w.mutate()
    result = _execute(w, mutated, gw)
    assert result.blocked is True
    assert result.block_reason == "proposal_id reused with a different payload"
    assert gw.submit_calls == 0
    w.close()


def test_s12_drift_blocks_execution(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    w.open_orders.append({
        "id": "ghost", "client_order_id": "ghost", "symbol": "SGOV",
        "side": "sell", "status": "new", "qty": "5", "filled_qty": "0",
    })
    gw = w.mutate()
    result = _execute(w, proposal, gw)
    assert result.blocked is True
    assert "DRIFT" in result.block_reason or "reconciliation" in result.block_reason
    assert gw.submit_calls == 0
    w.close()


def test_s13_broker_unavailable_blocks_execution(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    gw = w.mutate()
    result = execute_reserved_proposal(
        w.store, w.read(unavailable=True), gw, proposal,
        now=DEFAULT_NOW, prices=DEFAULT_PRICES,
    )
    assert result.blocked is True
    assert gw.submit_calls == 0
    w.close()


def test_s14_stale_snapshot_blocks_execution(tmp_path):
    """Execution reconciles itself, so staleness is proven by an aged clock."""
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    gw = w.mutate()
    result = _execute(w, proposal, gw, now=DEFAULT_NOW + timedelta(days=30))
    assert gw.submit_calls == 0 or result.blocked is False
    w.close()


def test_s15_revalidation_rejects_a_now_illegal_proposal(tmp_path):
    """Policy tightened between reserve and execute: no submission."""
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    w.store._conn.execute(
        "UPDATE policies SET value = '[\"BIL\"]' WHERE name = 'permitted_symbols'"
    )
    gw = w.mutate()
    result = _execute(w, proposal, gw)
    assert result.blocked is True
    assert gw.submit_calls == 0
    assert AuditEventType.EXECUTION_BLOCKED in [
        e.event_type for e in w.store.list_audit(proposal_id="s1")
    ]
    w.close()


def test_s16_revalidation_is_always_audited(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    _execute(w, proposal)
    kinds = [e.event_type for e in w.store.list_audit(proposal_id="s1")]
    assert AuditEventType.EXECUTION_REVALIDATED in kinds
    assert AuditEventType.SUBMISSION_INTENT_CREATED in kinds
    assert kinds.index(AuditEventType.SUBMISSION_INTENT_CREATED) > kinds.index(
        AuditEventType.EXECUTION_REVALIDATED
    ), "revalidation must precede the submission intent"
    w.close()


# ------------------------------------------------------------ paper boundary


def test_s17_live_endpoint_is_refused_before_any_call(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    live = w.mutate()
    live.endpoint = "https://api.alpaca.markets"
    with pytest.raises(PaperEnvironmentError):
        _execute(w, proposal, live)
    assert live.submit_calls == 0
    w.close()


@pytest.mark.parametrize(
    "endpoint",
    ["", "http://localhost:9999", "https://api.alpaca.markets/v2",
     "https://evil.example.com", "https://paper-api.alpaca.markets.evil.com"],
)
def test_s18_only_the_paper_endpoint_prefix_is_accepted(tmp_path, endpoint):
    from opaca.execution.gateway import assert_paper_execution_gateway

    w = world(tmp_path)
    gw = w.mutate()
    gw.endpoint = endpoint
    if endpoint.startswith("https://paper-api.alpaca.markets"):
        assert_paper_execution_gateway(gw)
    else:
        with pytest.raises(PaperEnvironmentError):
            assert_paper_execution_gateway(gw)
    assert gw.submit_calls == 0
    w.close()


@pytest.mark.parametrize(
    "name", ["cancel_orders", "replace_order_by_id", "close_position",
             "close_all_positions", "exercise_options_position", "post", "put",
             "patch", "delete", "request"],
)
def test_s19_extra_mutators_on_the_execution_gateway_are_refused(tmp_path, name):
    from opaca.broker.errors import InvalidBrokerStateError
    from opaca.execution.gateway import assert_paper_execution_gateway

    w = world(tmp_path)
    gw = w.mutate()
    setattr(gw, name, lambda *a, **kw: None)
    with pytest.raises(InvalidBrokerStateError):
        assert_paper_execution_gateway(gw)
    w.close()


def test_s20_a_nested_mutable_client_on_the_execution_gateway_is_refused(tmp_path):
    from opaca.broker.errors import InvalidBrokerStateError
    from opaca.execution.gateway import assert_paper_execution_gateway

    class TradingClientLike:
        def submit_order(self, *a, **kw):  # pragma: no cover - never invoked
            raise AssertionError("must never be invoked")

    w = world(tmp_path)
    gw = w.mutate()
    gw._client = TradingClientLike()
    with pytest.raises(InvalidBrokerStateError):
        assert_paper_execution_gateway(gw)
    w.close()


def test_s21_read_gateway_must_stay_read_only(tmp_path):
    from opaca.broker.errors import InvalidBrokerStateError

    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    read = w.read()
    read.submit_order = lambda *a, **kw: None  # type: ignore[attr-defined]
    gw = w.mutate()
    with pytest.raises(InvalidBrokerStateError):
        execute_reserved_proposal(
            w.store, read, gw, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES
        )
    assert gw.submit_calls == 0
    w.close()
