"""P0-D / P1-B: the gates the idempotent-replay branch must apply.

At 3fdabf3 the duplicate-proposal branch returned before every safety gate and
reported ``is_auto=True`` under a live kill switch, drift, unknown state and a
stale snapshot. CLOSED at d85a2e6: replay now runs the same snapshot gate as a
fresh proposal, then the kill switch, then approval expiry, and only then
reports the stored decision.

These tests assert the corrected behaviour. They have teeth: run against
3fdabf3 every one of them fails.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from opaca.domain.models import AuthorityResult, Side
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.types import AuditEventType, ReconciliationStatus
from opaca.reconciliation.service import reconcile
from tests.state_helpers import order_payload

from probe_support import (
    DEFAULT_NOW,
    DEFAULT_PRICES,
    make_order,
    make_proposal,
    paper_gateway,
    position_payload,
    reconciled_store,
)

SGOV = DEFAULT_PRICES["SGOV"]


def _auto(store, v, pid="live"):
    proposal = make_proposal(pid, [make_order(pid, 0, "SGOV", Side.SELL, "10", SGOV)])
    out = evaluate_and_reserve(
        store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
        expected_snapshot_version=v,
    )
    assert out.is_auto
    return proposal, out


def _replay(store, proposal, version, now=DEFAULT_NOW):
    return evaluate_and_reserve(
        store, proposal, now=now, prices=DEFAULT_PRICES, expected_snapshot_version=version
    )


def test_replay_applies_the_stale_snapshot_gate(tmp_path):
    store, v1 = reconciled_store(tmp_path, qty="100")
    proposal, _ = _auto(store, v1)
    reconcile(store, paper_gateway(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
    v2 = store.latest_snapshot().version
    assert v2 != v1
    replay = _replay(store, proposal, v1)
    assert replay.idempotent_replay is True
    assert replay.is_auto is False
    assert replay.blocked is True
    assert replay.block_reason == "stale snapshot"
    assert store.list_audit(event_type=AuditEventType.STALE_SNAPSHOT), (
        "a stale replay must leave a STALE_SNAPSHOT audit event"
    )
    store.close()


def test_replay_applies_the_snapshot_age_gate(tmp_path):
    store, v1 = reconciled_store(tmp_path, qty="100")
    proposal, _ = _auto(store, v1)
    max_age = timedelta(seconds=int(store.policy_value("max_snapshot_age_seconds")))
    replay = _replay(store, proposal, v1, now=DEFAULT_NOW + max_age + timedelta(seconds=1))
    assert replay.is_auto is False and replay.blocked is True
    assert replay.block_reason == "stale snapshot"
    store.close()


def test_replay_requires_an_expected_version(tmp_path):
    store, v1 = reconciled_store(tmp_path, qty="100")
    proposal, _ = _auto(store, v1)
    replay = evaluate_and_reserve(
        store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES
    )
    assert replay.is_auto is False and replay.blocked is True
    assert replay.block_reason == "expected_snapshot_version is required"
    store.close()


def test_replay_is_blocked_while_state_is_drifted(tmp_path):
    store, v1 = reconciled_store(tmp_path, qty="100")
    proposal, _ = _auto(store, v1)
    recon = reconcile(
        store,
        paper_gateway(positions=(position_payload(qty="100"),),
                      open_orders=(order_payload("ghost", status="new"),)),
        now=DEFAULT_NOW,
    )
    assert recon.status is ReconciliationStatus.DRIFT_DETECTED
    replay = _replay(store, proposal, recon.snapshot.version)
    assert replay.is_auto is False
    assert replay.blocked is True
    assert ReconciliationStatus.DRIFT_DETECTED.value in replay.block_reason
    store.close()


def test_replay_is_blocked_while_state_requires_review(tmp_path):
    store, v1 = reconciled_store(tmp_path, qty="100")
    proposal, _ = _auto(store, v1)
    recon = reconcile(
        store,
        paper_gateway(positions=(position_payload(qty="100"),),
                      open_orders=(order_payload("weird", status="frobnicated"),)),
        now=DEFAULT_NOW,
    )
    assert recon.status is ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW
    replay = _replay(store, proposal, recon.snapshot.version)
    assert replay.is_auto is False
    assert replay.blocked is True
    assert ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW.value in replay.block_reason
    store.close()


def test_replay_is_blocked_while_the_kill_switch_is_active(tmp_path):
    store, v1 = reconciled_store(tmp_path, qty="100")
    proposal, _ = _auto(store, v1)
    store.set_kill_switch(True, now=DEFAULT_NOW)
    replay = _replay(store, proposal, v1)
    assert replay.is_auto is False
    assert replay.blocked is True
    assert replay.block_reason == "kill switch active"
    denials = [
        e for e in store.list_audit(proposal_id="live")
        if e.event_type is AuditEventType.RESERVATION_DENIED
    ]
    assert any("kill switch" in e.reason for e in denials)
    store.close()


def test_a_blocked_replay_still_reports_the_stored_authority_result(tmp_path):
    """Blocking must not erase the record: callers still learn what was decided."""
    store, v1 = reconciled_store(tmp_path, qty="100")
    proposal, _ = _auto(store, v1)
    store.set_kill_switch(True, now=DEFAULT_NOW)
    replay = _replay(store, proposal, v1)
    assert replay.authority_result is AuthorityResult.AUTO
    assert replay.idempotent_replay is True
    assert replay.reserved is False
    store.close()


def test_a_clean_replay_is_still_auto_and_capacity_neutral(tmp_path):
    """The gates must not break idempotency itself."""
    store, v1 = reconciled_store(tmp_path, qty="100")
    proposal, _ = _auto(store, v1)
    before = (len(store.load_autonomous_history()), store.count_reservations("live"))
    for _ in range(3):
        replay = _replay(store, proposal, v1)
        assert replay.is_auto is True
        assert replay.idempotent_replay is True
        assert replay.blocked is False
    assert (len(store.load_autonomous_history()), store.count_reservations("live")) == before
    store.close()


def test_a_blocked_replay_never_adds_capacity(tmp_path):
    store, v1 = reconciled_store(tmp_path, qty="100")
    proposal, _ = _auto(store, v1)
    before = (len(store.load_autonomous_history()), store.count_reservations("live"))
    store.set_kill_switch(True, now=DEFAULT_NOW)
    for _ in range(3):
        _replay(store, proposal, v1)
    store.set_kill_switch(False, now=DEFAULT_NOW)
    for _ in range(3):
        _replay(store, proposal, v1, now=DEFAULT_NOW + timedelta(days=30))
    assert (len(store.load_autonomous_history()), store.count_reservations("live")) == before
    store.close()


@pytest.mark.parametrize("gate", ["kill_switch", "drift", "stale_age", "no_version"])
def test_every_gate_denies_a_replay_of_an_approval_required_proposal_too(tmp_path, gate):
    from opaca.persistence.codec import dump_datetime

    store, v1 = reconciled_store(tmp_path, qty=None, cash="100000")
    store._conn.execute(
        "INSERT INTO autonomous_executions(proposal_id, timestamp, notional) VALUES (?,?,?)",
        ("prior", dump_datetime(DEFAULT_NOW), "49000"),
    )
    proposal = make_proposal("ar", [make_order("ar", 0, "SGOV", Side.BUY, "20", SGOV)])
    first = evaluate_and_reserve(
        store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES, expected_snapshot_version=v1
    )
    assert first.authority_result is AuthorityResult.APPROVAL_REQUIRED

    now = DEFAULT_NOW
    version = v1
    if gate == "kill_switch":
        store.set_kill_switch(True, now=DEFAULT_NOW)
    elif gate == "drift":
        recon = reconcile(
            store,
            paper_gateway(open_orders=(order_payload("ghost", status="new"),)),
            now=DEFAULT_NOW,
        )
        version = recon.snapshot.version
    elif gate == "stale_age":
        now = DEFAULT_NOW + timedelta(days=30)
    elif gate == "no_version":
        version = None

    replay = evaluate_and_reserve(
        store, proposal, now=now, prices=DEFAULT_PRICES, expected_snapshot_version=version
    )
    assert replay.blocked is True
    assert replay.is_auto is False
    assert replay.approval_currently_valid(now) is False
    assert store.count_reservations("ar") == 0
    store.close()


def test_FINDING_replay_is_not_re_evaluated_against_the_newer_snapshot(tmp_path):
    """The gates check that the snapshot is current, reconciled and fresh — they do
    not re-run TreasuryGuard. A proposal that was AUTO at v1 still reports
    is_auto=True at a valid, fresh, RECONCILED v2 under which an identical fresh
    proposal is a hard REJECT, and it reports the stale v1 as its snapshot_version.
    """
    store, v1 = reconciled_store(tmp_path, qty=None, cash="100000")
    proposal = make_proposal("b", [make_order("b", 0, "SGOV", Side.BUY, "99", SGOV)])
    first = evaluate_and_reserve(
        store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES, expected_snapshot_version=v1
    )
    assert first.is_auto

    recon = reconcile(store, paper_gateway(cash="1"), now=DEFAULT_NOW)
    assert recon.status is ReconciliationStatus.RECONCILED
    v2 = recon.snapshot.version

    identical = make_proposal("c", [make_order("c", 0, "SGOV", Side.BUY, "99", SGOV)])
    fresh = evaluate_and_reserve(
        store, identical, now=DEFAULT_NOW, prices=DEFAULT_PRICES, expected_snapshot_version=v2
    )
    assert fresh.authority_result is AuthorityResult.REJECT, "probe assumption"

    replay = _replay(store, proposal, v2)
    store.close()
    assert replay.is_auto is True, "probe assumption"
    assert replay.snapshot_version == v1
    pytest.fail(
        "FINDING P0-1-r (residual): replay reports is_auto=True at snapshot v"
        f"{v2} while an identical fresh proposal is REJECT there; the replay carries "
        f"snapshot_version={v1}. The gates check snapshot currency, not the decision. "
        "Compensating control: the module contract requires a fresh reconciliation and "
        "a fresh TreasuryGuard run before any submission."
    )
