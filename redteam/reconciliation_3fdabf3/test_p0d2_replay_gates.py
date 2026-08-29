"""P0-D / P1-B: what the idempotent-replay branch skips."""

from __future__ import annotations

import pytest
from opaca.domain.models import AuthorityResult, Side
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.types import AuditEventType, ReconciliationStatus
from opaca.reconciliation.service import reconcile

from probe_support import (
    DEFAULT_NOW,
    DEFAULT_PRICES,
    make_order,
    make_proposal,
    paper_gateway,
    position_payload,
    reconciled_store,
)
from tests.state_helpers import order_payload

SGOV = DEFAULT_PRICES["SGOV"]


def _auto(store, v, pid="live"):
    p = make_proposal(pid, [make_order(pid, 0, "SGOV", Side.SELL, "10", SGOV)])
    out = evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                               expected_snapshot_version=v)
    assert out.is_auto
    return p, out


def test_FINDING_replay_bypasses_the_stale_snapshot_gate(tmp_path):
    store, v1 = reconciled_store(tmp_path, qty="100")
    proposal, first = _auto(store, v1)
    reconcile(store, paper_gateway(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
    v2 = store.latest_snapshot().version
    assert v2 != v1
    replay = evaluate_and_reserve(store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                  expected_snapshot_version=v1)
    stale_events = store.list_audit(event_type=AuditEventType.STALE_SNAPSHOT)
    store.close()
    assert replay.idempotent_replay is True
    assert replay.is_auto is True, "probe assumption"
    assert stale_events == ()
    pytest.fail(
        f"FINDING P0-D-1: the duplicate-proposal branch returns before the "
        f"reconciliation-status and stale-snapshot gates. The replay reports "
        f"is_auto=True carrying snapshot_version={replay.snapshot_version} while the "
        f"authoritative snapshot is v{v2}, and no STALE_SNAPSHOT audit is written"
    )


def test_FINDING_replay_reports_auto_while_state_is_drifted(tmp_path):
    store, v1 = reconciled_store(tmp_path, qty="100")
    proposal, _ = _auto(store, v1)
    recon = reconcile(
        store,
        paper_gateway(positions=(position_payload(qty="100"),),
                      open_orders=(order_payload("ghost", status="new"),)),
        now=DEFAULT_NOW,
    )
    assert recon.status is ReconciliationStatus.DRIFT_DETECTED
    replay = evaluate_and_reserve(store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                  expected_snapshot_version=recon.snapshot.version)
    store.close()
    assert replay.idempotent_replay is True
    assert replay.is_auto is True, "probe assumption"
    pytest.fail(
        "FINDING P0-D-2: replaying an already-AUTO proposal while the latest "
        "reconciliation is DRIFT_DETECTED still returns is_auto=True and blocked=False; "
        "any executor keyed on OrchestrationResult.is_auto would act on drifted state"
    )


def test_FINDING_replay_reports_auto_while_state_requires_review(tmp_path):
    store, v1 = reconciled_store(tmp_path, qty="100")
    proposal, _ = _auto(store, v1)
    recon = reconcile(
        store,
        paper_gateway(positions=(position_payload(qty="100"),),
                      open_orders=(order_payload("weird", status="frobnicated"),)),
        now=DEFAULT_NOW,
    )
    assert recon.status is ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW
    replay = evaluate_and_reserve(store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                  expected_snapshot_version=recon.snapshot.version)
    store.close()
    assert replay.is_auto is True, "probe assumption"
    pytest.fail(
        "FINDING P0-D-3: UNKNOWN_REQUIRES_REVIEW does not suppress an idempotent "
        "replay's is_auto=True; uncertainty remains an executable-looking state on "
        "the replay path"
    )


def test_FINDING_replay_reports_auto_while_kill_switch_is_active(tmp_path):
    store, v1 = reconciled_store(tmp_path, qty="100")
    proposal, _ = _auto(store, v1)
    store.set_kill_switch(True, now=DEFAULT_NOW)
    replay = evaluate_and_reserve(store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                  expected_snapshot_version=v1)
    store.close()
    assert replay.is_auto is True, "probe assumption"
    pytest.fail(
        "FINDING P0-D-4: with the kill switch ACTIVE, replaying an already-AUTO "
        "proposal still returns is_auto=True (the replay branch never re-evaluates "
        "CHECK-00)"
    )


def test_replay_does_not_add_capacity_even_though_it_reports_auto(tmp_path):
    """Control: the replay is at least still capacity-neutral."""
    store, v1 = reconciled_store(tmp_path, qty="100")
    proposal, _ = _auto(store, v1)
    before = (len(store.load_autonomous_history()), store.count_reservations("live"))
    for _ in range(3):
        evaluate_and_reserve(store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                             expected_snapshot_version=v1)
    assert (len(store.load_autonomous_history()), store.count_reservations("live")) == before
    store.close()
