"""P1: state machine, recovery, approvals, audit, adapters."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from opaca.broker.errors import InvalidBrokerStateError
from opaca.domain.models import AuthorityResult, Side
from opaca.execution.errors import ExecutionBlockedError, IllegalTransitionError
from opaca.execution.service import (
    cancel_remaining,
    execute_reserved_proposal,
    grant_human_approval,
    recover_open_executions,
    recover_proposal,
)
from opaca.execution.states import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    map_broker_status,
    validate_transition,
)
from opaca.persistence.codec import dump_datetime
from opaca.persistence.types import AuditEventType, ExecutionState, ReconciliationStatus
from opaca.reconciliation.service import reconcile

from p3_support import (
    DEFAULT_NOW,
    DEFAULT_PRICES,
    SGOV,
    active_sell_qty,
    buy,
    make_order,
    make_proposal,
    reserve,
    sell,
    world,
)


def _execute(w, proposal, gw=None, **kw):
    return execute_reserved_proposal(
        w.store, w.read(), gw if gw is not None else w.mutate(**kw),
        proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
    )


# ------------------------------------------------------------ state machine


def test_t01_terminal_states_are_absorbing(tmp_path):
    for state in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[state] == frozenset(), state
        for target in ExecutionState:
            if target is state:
                continue
            with pytest.raises(IllegalTransitionError):
                validate_transition(state, target)


def test_t02_no_transition_goes_backwards_into_submitting_or_ready(tmp_path):
    for source, targets in LEGAL_TRANSITIONS.items():
        assert ExecutionState.READY not in targets, source
        if source is ExecutionState.READY:
            continue  # READY -> SUBMITTING is the one legitimate entry edge
        assert ExecutionState.SUBMITTING not in targets, source


def test_t03_partially_filled_cannot_regress_to_submitted(tmp_path):
    with pytest.raises(IllegalTransitionError):
        validate_transition(ExecutionState.PARTIALLY_FILLED, ExecutionState.SUBMITTED)


def test_t04_every_open_state_can_reach_unknown(tmp_path):
    from opaca.execution.states import OPEN_RECOVERY_STATES

    for state in OPEN_RECOVERY_STATES:
        if state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION:
            continue
        validate_transition(state, ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION)


def test_t05_unmapped_broker_status_is_unknown_not_terminal(tmp_path):
    for status in ("frobnicated", "", "PENDING_SOMETHING", "done_for_day", "held", "calculated"):
        assert map_broker_status(status) is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION, status


def test_t06_pending_cancel_maps_to_cancel_pending(tmp_path):
    assert map_broker_status("pending_cancel") is ExecutionState.CANCEL_PENDING


def test_t07_expired_is_terminal_cancelled(tmp_path):
    assert map_broker_status("expired") is ExecutionState.CANCELLED


# ------------------------------------------------------------ recovery


def test_t08_unknown_cannot_be_cancelled_until_recovered(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    gw = w.mutate(timeout_before_accept=True)
    _execute(w, proposal, gw)
    with pytest.raises(ExecutionBlockedError):
        cancel_remaining(w.store, w.read(), gw, proposal.legs[0].client_order_id, now=DEFAULT_NOW)
    assert gw.cancel_calls == 0
    w.close()


def test_t09_lookup_unavailable_keeps_the_order_unknown(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    gw = w.mutate(timeout_after_accept=True)
    _execute(w, proposal, gw)
    result = recover_proposal(
        w.store, w.read(lookup_unavailable=True), "s1", now=DEFAULT_NOW
    )
    assert result.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
    assert result.blocked is True
    assert active_sell_qty(w.store) == Decimal("10"), "capacity retained under uncertainty"
    w.close()


def test_t10_an_order_absent_at_the_broker_stays_unknown(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    gw = w.mutate(timeout_after_accept=True)
    _execute(w, proposal, gw)
    w.orders.clear()  # the broker no longer knows the order
    result = recover_proposal(w.store, w.read(), "s1", now=DEFAULT_NOW)
    assert result.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
    assert active_sell_qty(w.store) == Decimal("10")
    w.close()


def test_t11_unknown_execution_blocks_later_reconciliation(tmp_path):
    """An UNKNOWN order must make the whole account non-tradable."""
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    _execute(w, proposal, timeout_before_accept=True)
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    assert recon.status is not ReconciliationStatus.RECONCILED
    other = sell("s2", "5")
    _, out = reserve_or_none(w, other)
    assert out is None or not out.is_auto
    w.close()


def reserve_or_none(w, proposal):
    from opaca.orchestration.reserve import evaluate_and_reserve

    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    if recon.status is not ReconciliationStatus.RECONCILED or recon.snapshot is None:
        return None, None
    return recon.snapshot.version, evaluate_and_reserve(
        w.store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
        expected_snapshot_version=recon.snapshot.version,
    )


def test_t12_restart_recovery_covers_every_open_proposal(tmp_path):
    w = world(tmp_path, qty="100")
    for i, qty in enumerate(["5", "6"]):
        p = sell(f"s{i}", qty)
        reserve(w, p)
        _execute(w, p, fill_on_submit=False)
    results = recover_open_executions(w.store, w.read(), now=DEFAULT_NOW)
    assert {r.proposal_id for r in results} == {"s0", "s1"}
    assert all(r.submitted is False for r in results)
    w.close()


# ------------------------------------------------------------ approvals


def _escalated(w):
    w.store._conn.execute(
        "INSERT INTO autonomous_executions(proposal_id, timestamp, notional) VALUES (?,?,?)",
        ("prior", dump_datetime(DEFAULT_NOW), "49000"),
    )
    proposal = buy("ar", "20")
    _, out = reserve(w, proposal)
    assert out.authority_result is AuthorityResult.APPROVAL_REQUIRED, out.authority_result
    return proposal


def test_t13_approval_required_cannot_execute_without_a_grant(tmp_path):
    w = world(tmp_path, qty="0", cash="100000")
    proposal = _escalated(w)
    gw = w.mutate()
    result = _execute(w, proposal, gw)
    assert result.blocked is True
    assert result.block_reason == "approval required and not granted"
    assert gw.submit_calls == 0
    w.close()


def test_t14_a_grant_is_bound_to_the_payload_hash(tmp_path):
    w = world(tmp_path, qty="0", cash="100000")
    proposal = _escalated(w)
    grant_human_approval(w.store, proposal, now=DEFAULT_NOW)
    mutated = buy("ar", "21")
    gw = w.mutate()
    result = _execute(w, mutated, gw)
    assert result.blocked is True
    assert gw.submit_calls == 0
    w.close()


def test_t15_a_grant_for_an_unknown_proposal_is_refused(tmp_path):
    w = world(tmp_path, qty="0", cash="100000")
    with pytest.raises(ExecutionBlockedError):
        grant_human_approval(w.store, buy("nope", "1"), now=DEFAULT_NOW)
    w.close()


def test_t16_an_expired_approval_cannot_be_granted(tmp_path):
    w = world(tmp_path, qty="0", cash="100000")
    proposal = _escalated(w)
    with pytest.raises(ExecutionBlockedError):
        grant_human_approval(w.store, proposal, now=DEFAULT_NOW + timedelta(seconds=301))
    w.close()


def test_t17_a_grant_never_overrides_a_hard_reject(tmp_path):
    w = world(tmp_path, qty="0", cash="100000")
    proposal = _escalated(w)
    grant_human_approval(w.store, proposal, now=DEFAULT_NOW)
    w.store._conn.execute(
        "UPDATE policies SET value = '[\"BIL\"]' WHERE name = 'permitted_symbols'"
    )
    gw = w.mutate()
    result = _execute(w, proposal, gw)
    assert result.blocked is True
    assert gw.submit_calls == 0, "human approval must not override a policy REJECT"
    w.close()


def test_t18_granting_is_idempotent(tmp_path):
    w = world(tmp_path, qty="0", cash="100000")
    proposal = _escalated(w)
    for _ in range(3):
        grant_human_approval(w.store, proposal, now=DEFAULT_NOW)
    rows = w.store._conn.execute(
        "SELECT COUNT(*) AS n FROM approval_grants WHERE proposal_id='ar'"
    ).fetchone()["n"]
    assert rows == 1
    w.close()


def test_t19_a_granted_approval_can_execute(tmp_path):
    w = world(tmp_path, qty="0", cash="100000")
    proposal = _escalated(w)
    grant_human_approval(w.store, proposal, now=DEFAULT_NOW)
    gw = w.mutate()
    result = _execute(w, proposal, gw)
    assert result.blocked is False
    assert gw.submit_calls == 1
    assert AuditEventType.HUMAN_APPROVAL_GRANTED in [
        e.event_type for e in w.store.list_audit(proposal_id="ar")
    ]
    w.close()


# ------------------------------------------------------------ audit + adapters


def test_t20_a_filled_order_leaves_a_complete_audit_trail(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    _execute(w, proposal)
    kinds = [e.event_type for e in w.store.list_audit(proposal_id="s1")]
    for required in (
        AuditEventType.EXECUTION_REVALIDATED,
        AuditEventType.SUBMISSION_INTENT_CREATED,
        AuditEventType.ORDER_SUBMITTED,
        AuditEventType.ORDER_ACKNOWLEDGED,
        AuditEventType.FULL_FILL,
        AuditEventType.SETTLEMENT_CREATED,
        AuditEventType.RESERVATION_RELEASED,
    ):
        assert required in kinds, required
    w.close()


def test_t21_audit_carries_the_broker_identity_and_no_secrets(tmp_path):
    import re

    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    _execute(w, proposal)
    coid = proposal.legs[0].client_order_id
    events = w.store.list_audit(proposal_id="s1")
    assert any(e.broker_identifiers == coid for e in events)
    secret = re.compile(r"(api[_-]?key|secret|token|password|PK[A-Z0-9]{10,})", re.IGNORECASE)
    for e in events:
        assert not secret.search(f"{e.reason} {e.detail} {e.broker_identifiers}")
    w.close()


def test_t22_a_sell_fill_creates_exactly_one_settlement_event_per_increment(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    gw = w.mutate(partial_fill_qty=Decimal("4"))
    _execute(w, proposal, gw)
    first = w.store.load_settlement_events()
    assert len(first) == 1
    for _ in range(3):
        recover_proposal(w.store, w.read(), "s1", now=DEFAULT_NOW)
    assert len(w.store.load_settlement_events()) == 1, "recovery must not duplicate proceeds"
    w.close()


def test_t23_settlement_proceeds_never_exceed_the_filled_notional(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    _execute(w, proposal)
    total = sum((e.amount for e in w.store.load_settlement_events()), Decimal("0"))
    assert total <= Decimal("10") * SGOV
    w.close()


@pytest.mark.parametrize(
    "bad",
    [
        {"status": "filled", "qty": "10", "filled_qty": "50"},
        {"status": "filled", "qty": "10", "filled_qty": "NaN"},
        {"status": "filled", "qty": "10", "filled_qty": "-1"},
        {"status": "new", "qty": "ten", "filled_qty": "0"},
        {"status": "filled", "qty": "10", "filled_qty": "10", "side": "sideways"},
    ],
)
def test_t24_a_corrupt_submit_response_never_becomes_a_clean_fill(tmp_path, bad):
    """The broker's own acknowledgement is untrusted input."""
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    coid = proposal.legs[0].client_order_id
    gw = w.mutate()

    def corrupt(request):
        gw.submit_calls += 1
        payload = {
            "id": "broker-x", "client_order_id": coid, "symbol": "SGOV",
            "side": "sell", "filled_avg_price": "100.69",
        }
        payload.update(bad)
        gw.orders[coid] = payload
        return payload

    gw.submit_order = corrupt  # type: ignore[method-assign]
    state = None
    try:
        state = _execute(w, proposal, gw).state
    except Exception:  # noqa: BLE001, S110
        pass
    record = w.store.get_execution_order(coid)
    assert state is not ExecutionState.FILLED or record.filled_quantity <= Decimal("10")
    if record is not None:
        assert record.filled_quantity <= record.quantity
    w.close()


def test_t25_a_corrupt_filled_avg_price_does_not_corrupt_proceeds(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    coid = proposal.legs[0].client_order_id
    gw = w.mutate()

    def corrupt(request):
        gw.submit_calls += 1
        payload = {
            "id": "broker-x", "client_order_id": coid, "symbol": "SGOV", "side": "sell",
            "status": "filled", "qty": "10", "filled_qty": "10",
            "filled_avg_price": "-1",
        }
        gw.orders[coid] = payload
        return payload

    gw.submit_order = corrupt  # type: ignore[method-assign]
    try:
        _execute(w, proposal, gw)
    except Exception:  # noqa: BLE001, S110
        pass
    for event in w.store.load_settlement_events():
        assert event.amount >= 0
    w.close()


def test_no_bare_assert_in_the_production_package(tmp_path):
    """CLOSED at cd3dc86 (was P2-1). The treasury-core control is restored: zero
    `assert` statements in backend/opaca/, so behaviour is identical under -O."""
    import ast
    import pathlib

    import opaca

    root = pathlib.Path(opaca.__file__).resolve().parent
    modules = sorted(root.rglob("*.py"))
    found = [
        f"{p.relative_to(root)}:{node.lineno}"
        for p in modules
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
        if isinstance(node, ast.Assert)
    ]
    assert modules
    assert found == [], found


def test_the_replacement_raises_instead_of_asserting(tmp_path):
    """The invariant that the assert used to guard is now an explicit raise."""
    import pathlib

    import opaca.execution.service

    source = pathlib.Path(
        opaca.execution.service.__file__
    ).read_text(encoding="utf-8")
    assert "ExecutionInvariantError" in source
    assert "assert last is not None" not in source
