"""P1-E audit trail; P1-F APPROVAL_REQUIRED never consumes executable capacity."""

from __future__ import annotations

import json
import re
from decimal import Decimal

import pytest
from opaca.domain.models import AuthorityResult, Side
from opaca.orchestration.reserve import evaluate_and_reserve, read_reconcile_evaluate_reserve
from opaca.persistence.codec import dump_datetime
from opaca.persistence.types import AuditEventType, ProposalRecordStatus, ReconciliationStatus
from opaca.reconciliation.service import reconcile

from probe_support import (
    DEFAULT_NOW,
    DEFAULT_PRICES,
    make_order,
    make_proposal,
    paper_gateway,
    position_payload,
    reconciled_store,
    temp_store,
)
from tests.state_helpers import PHASE1_ASSETS, account_payload, order_payload

SGOV = DEFAULT_PRICES["SGOV"]
SECRET_RE = re.compile(
    r"(api[_-]?key|secret|token|password|PK[A-Z0-9]{10,}|account_number)", re.IGNORECASE
)


def _types(store, proposal_id=None):
    return [e.event_type for e in store.list_audit(proposal_id=proposal_id)]


def test_e1_auto_reserved_emits_reservation_audit(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    p = make_proposal("a", [make_order("a", 0, "SGOV", Side.SELL, "10", SGOV)])
    assert evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                expected_snapshot_version=v).is_auto
    kinds = _types(store, "a")
    assert AuditEventType.PROPOSAL_EVALUATED in kinds
    assert AuditEventType.RESERVATION_CREATED in kinds
    store.close()


def test_e2_reject_emits_denial_audit(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    p = make_proposal("r", [make_order("r", 0, "SGOV", Side.SELL, "500", SGOV)])
    out = evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                               expected_snapshot_version=v)
    assert out.authority_result is AuthorityResult.REJECT
    kinds = _types(store, "r")
    assert AuditEventType.RESERVATION_DENIED in kinds
    assert AuditEventType.POLICY_REJECTED in kinds
    store.close()


def test_e3_approval_required_emits_its_own_audit(tmp_path):
    store, v = reconciled_store(tmp_path, qty=None, cash="100000")
    store._conn.execute(
        "INSERT INTO autonomous_executions(proposal_id, timestamp, notional) VALUES (?,?,?)",
        ("prior", dump_datetime(DEFAULT_NOW), "49000"),
    )
    p = make_proposal("ar", [make_order("ar", 0, "SGOV", Side.BUY, "20", SGOV)])
    out = evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                               expected_snapshot_version=v)
    assert out.authority_result is AuthorityResult.APPROVAL_REQUIRED
    assert AuditEventType.APPROVAL_REQUIRED in _types(store, "ar")
    store.close()


def test_e4_stale_snapshot_emits_stale_audit(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    reconcile(store, paper_gateway(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
    p = make_proposal("s", [make_order("s", 0, "SGOV", Side.SELL, "10", SGOV)])
    evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                         expected_snapshot_version=v)
    assert AuditEventType.STALE_SNAPSHOT in _types(store, "s")
    store.close()


def test_e5_drift_and_unknown_and_broker_failure_are_audited(tmp_path):
    store = temp_store(tmp_path)
    reconcile(store, paper_gateway(unavailable=True), now=DEFAULT_NOW)
    assert AuditEventType.BROKER_UNAVAILABLE in _types(store)
    reconcile(store, paper_gateway(positions=(position_payload(qty="100"),),
                                   open_orders=(order_payload("g", status="new"),)),
              now=DEFAULT_NOW)
    assert AuditEventType.DRIFT_DETECTED in _types(store)
    reconcile(store, paper_gateway(positions=(position_payload(qty="100"),),
                                   open_orders=(order_payload("g2", status="frobnicated"),)),
              now=DEFAULT_NOW)
    assert AuditEventType.UNKNOWN_REQUIRES_REVIEW in _types(store)
    bad = dict(account_payload())
    bad["cash"] = "NaN"
    from opaca.broker.gateway import FakeAlpacaGateway

    reconcile(store, FakeAlpacaGateway(account=bad, assets=PHASE1_ASSETS), now=DEFAULT_NOW)
    assert AuditEventType.INVALID_BROKER_STATE in _types(store)
    store.close()


def test_e6_reservation_denied_on_unreconciled_snapshot(tmp_path):
    store, _ = reconciled_store(tmp_path, qty="100")
    r = reconcile(store, paper_gateway(positions=(position_payload(qty="100"),),
                                       open_orders=(order_payload("g", status="new"),)),
                  now=DEFAULT_NOW)
    p = make_proposal("blocked", [make_order("blocked", 0, "SGOV", Side.SELL, "10", SGOV)])
    evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                         expected_snapshot_version=r.snapshot.version)
    assert AuditEventType.RESERVATION_DENIED in _types(store, "blocked")
    store.close()


def test_e7_audit_contains_no_credentials_or_account_identifiers(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    p = make_proposal("a", [make_order("a", 0, "SGOV", Side.SELL, "10", SGOV)])
    evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                         expected_snapshot_version=v)
    for event in store.list_audit():
        blob = f"{event.reason} {event.detail} {event.broker_identifiers}"
        assert not SECRET_RE.search(blob), blob
    snap = store.latest_snapshot()
    diagnostics = json.loads(snap.diagnostics)
    for banned in ("id", "account_number", "api_key", "secret", "secret_key", "key_id", "token"):
        assert banned not in diagnostics
    assert not SECRET_RE.search(snap.diagnostics), snap.diagnostics
    store.close()


def test_e8_audit_failure_does_not_silently_permit_execution(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    original = store.record_audit

    def failing(event_type, *a, **kw):
        if event_type is AuditEventType.RESERVATION_CREATED:
            raise RuntimeError("audit sink down")
        return original(event_type, *a, **kw)

    store.record_audit = failing  # type: ignore[method-assign]
    p = make_proposal("noaudit", [make_order("noaudit", 0, "SGOV", Side.SELL, "10", SGOV)])
    with pytest.raises(RuntimeError):
        evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                             expected_snapshot_version=v)
    store.record_audit = original  # type: ignore[method-assign]
    assert store.get_proposal("noaudit") is None
    assert store.count_reservations("noaudit") == 0
    assert store.load_autonomous_history() == ()
    store.close()


# --------------------------------------------------------------- P1-F


def test_f1_approval_required_reserves_nothing(tmp_path):
    store, v = reconciled_store(tmp_path, qty=None, cash="100000")
    store._conn.execute(
        "INSERT INTO autonomous_executions(proposal_id, timestamp, notional) VALUES (?,?,?)",
        ("prior", dump_datetime(DEFAULT_NOW), "49000"),
    )
    p = make_proposal("ar", [make_order("ar", 0, "SGOV", Side.BUY, "20", SGOV)])
    out = evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                               expected_snapshot_version=v)
    assert out.authority_result is AuthorityResult.APPROVAL_REQUIRED
    assert out.reserved is False
    assert store.count_reservations("ar") == 0
    history = store.load_autonomous_history()
    assert [h.proposal_id if hasattr(h, "proposal_id") else None for h in history] or True
    assert len(history) == 1  # only the pre-seeded prior execution
    record = store.get_proposal("ar")
    assert record.status is ProposalRecordStatus.APPROVAL_REQUIRED
    assert record.expires_at is not None
    assert record.snapshot_version == v
    approval = store._conn.execute(
        "SELECT payload_hash, snapshot_version, expires_at FROM approvals WHERE proposal_id='ar'"
    ).fetchone()
    assert approval["payload_hash"] == out.proposal_hash
    assert approval["snapshot_version"] == v
    assert approval["expires_at"] is not None
    store.close()


def test_f2_approval_required_does_not_shrink_later_auto_capacity(tmp_path):
    """An escalated proposal must not consume deployable cash."""
    store, v = reconciled_store(tmp_path, qty=None, cash="100000")
    store._conn.execute(
        "INSERT INTO autonomous_executions(proposal_id, timestamp, notional) VALUES (?,?,?)",
        ("prior", dump_datetime(DEFAULT_NOW), "49000"),
    )
    escalated = make_proposal("esc", [make_order("esc", 0, "SGOV", Side.BUY, "20", SGOV)])
    out = evaluate_and_reserve(store, escalated, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                               expected_snapshot_version=v)
    assert out.authority_result is AuthorityResult.APPROVAL_REQUIRED
    reserved_cash = [r for r in store.active_reservations() if r.kind.value == "CASH_DEPLOYMENT"]
    assert reserved_cash == []
    store.close()


def test_f3_no_execution_path_consumes_an_approval(tmp_path):
    """Architectural: nothing in the package reads the approvals table to execute."""
    import subprocess
    from pathlib import Path

    import opaca

    root = Path(opaca.__file__).resolve().parent
    hits = subprocess.run(
        ["grep", "-rn", "approvals", str(root)], capture_output=True, text=True
    ).stdout.strip().splitlines()
    writes = [h for h in hits if "INSERT INTO approvals" in h]
    reads = [h for h in hits if "FROM approvals" in h or "UPDATE approvals" in h]
    assert len(writes) == 1
    assert reads == [], reads


def test_f4_human_approval_cannot_override_hard_reject(tmp_path):
    """A REJECT is persisted as REJECTED with no approval row and no expiry."""
    store, v = reconciled_store(tmp_path, qty="100")
    p = make_proposal("hard", [make_order("hard", 0, "SGOV", Side.SELL, "500", SGOV)])
    out = evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                               expected_snapshot_version=v)
    assert out.authority_result is AuthorityResult.REJECT
    record = store.get_proposal("hard")
    assert record.status is ProposalRecordStatus.REJECTED
    assert record.expires_at is None
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM approvals WHERE proposal_id='hard'"
    ).fetchone()["n"] == 0
    assert store.count_reservations("hard") == 0
    store.close()


def test_f5_kill_switch_reject_is_also_unapprovable(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    store.set_kill_switch(True, now=DEFAULT_NOW)
    p = make_proposal("ks", [make_order("ks", 0, "SGOV", Side.SELL, "10", SGOV)])
    out = evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                               expected_snapshot_version=v)
    assert out.authority_result is AuthorityResult.REJECT
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM approvals WHERE proposal_id='ks'"
    ).fetchone()["n"] == 0
    store.close()


def test_FINDING_approval_records_expiry_but_nothing_enforces_it(tmp_path):
    """The approvals row carries expires_at and snapshot_version, but a replay of the
    same proposal_id after expiry returns the stored decision unchanged, with no
    freshness re-check, because idempotent replay short-circuits before evaluation."""
    from datetime import timedelta

    store, v = reconciled_store(tmp_path, qty=None, cash="100000")
    store._conn.execute(
        "INSERT INTO autonomous_executions(proposal_id, timestamp, notional) VALUES (?,?,?)",
        ("prior", dump_datetime(DEFAULT_NOW), "49000"),
    )
    p = make_proposal("exp", [make_order("exp", 0, "SGOV", Side.BUY, "20", SGOV)])
    first = evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                 expected_snapshot_version=v)
    assert first.authority_result is AuthorityResult.APPROVAL_REQUIRED
    later = DEFAULT_NOW + timedelta(days=7)
    replay = evaluate_and_reserve(store, p, now=later, prices=DEFAULT_PRICES,
                                  expected_snapshot_version=v)
    store.close()
    assert replay.idempotent_replay is True
    assert replay.authority_result is AuthorityResult.APPROVAL_REQUIRED
    pytest.fail(
        "FINDING P1-F-1: replaying an APPROVAL_REQUIRED proposal 7 days after its "
        "300s expiry returns the stored decision verbatim; expires_at is recorded but "
        "no code path reads or enforces it"
    )
