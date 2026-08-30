"""P2: architecture / test quality. Non-blocking observations, each demonstrated."""

from __future__ import annotations

import ast
from pathlib import Path

import opaca
import pytest
from opaca.domain.models import AuthorityResult, Side
from opaca.orchestration.reserve import evaluate_and_reserve, proposal_hash
from opaca.persistence.types import ReservationStatus

from probe_support import (
    DEFAULT_NOW,
    DEFAULT_PRICES,
    make_order,
    make_proposal,
    reconciled_store,
)

SGOV = DEFAULT_PRICES["SGOV"]
ROOT = Path(opaca.__file__).resolve().parent
REPO = ROOT.parents[1]


def test_reservations_are_released_against_proven_disposition(tmp_path):
    """CLOSED at 79a7b1b (was Phase 2 P2-1). Reservations are no longer permanent:
    the execution layer resizes them against proven fills and releases them on a
    proven terminal state. UNKNOWN and SUBMITTING still retain capacity."""
    from opaca.execution.service import execute_reserved_proposal
    from opaca.persistence.types import ReservationStatus

    text = "\n".join(p.read_text(encoding="utf-8") for p in ROOT.rglob("*.py"))
    assert "ReservationStatus.RELEASED" in text
    assert "UPDATE reservations" in text

    store, v = reconciled_store(tmp_path, qty="100")
    p = make_proposal("s", [make_order("s", 0, "SGOV", Side.SELL, "10", SGOV)])
    assert evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                expected_snapshot_version=v).is_auto
    assert any(r.status is ReservationStatus.ACTIVE for r in store.active_reservations())
    store.close()


def test_an_auto_sell_blocks_an_opposing_buy_only_while_it_is_live(tmp_path):
    """CLOSED at 79a7b1b (was Phase 2 P2-2). A live SELL reservation still blocks an
    opposing BUY of the same symbol - that is CHECK-10 doing its job - but the block
    is no longer permanent: once the sell reaches a proven terminal state and its
    reservation is released, the opposing buy is AUTO again."""
    store, v = reconciled_store(tmp_path, qty="100", cash="100000")
    sell_proposal = make_proposal("s", [make_order("s", 0, "SGOV", Side.SELL, "1", SGOV)])
    assert evaluate_and_reserve(store, sell_proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                expected_snapshot_version=v).is_auto
    blocked = make_proposal("b0", [make_order("b0", 0, "SGOV", Side.BUY, "1", SGOV)])
    while_live = evaluate_and_reserve(store, blocked, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                      expected_snapshot_version=v)
    assert while_live.authority_result is AuthorityResult.REJECT, (
        "an opposing buy against a live sell must still be refused"
    )
    store.close()
    # the release half of this invariant is asserted end to end by the Phase 3 suite
    # (redteam/paper_execution_79a7b1b/test_p0_reservations.py::test_v01/v13).


def test_FINDING_proposal_hash_is_sensitive_to_leg_list_order(tmp_path):
    """Two byte-identical logical proposals whose legs are listed in a different order
    hash differently, so a legitimate retry is permanently blocked instead of replayed."""
    a = make_order("m", 0, "SGOV", Side.SELL, "1", SGOV)
    b = make_order("m", 1, "BIL", Side.SELL, "1", DEFAULT_PRICES["BIL"])
    forward = make_proposal("m", [a, b])
    reversed_ = make_proposal("m", [b, a])
    assert proposal_hash(forward) != proposal_hash(reversed_), "probe assumption"
    store, v = reconciled_store(tmp_path, qty="100")
    first = evaluate_and_reserve(store, forward, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                 expected_snapshot_version=v)
    retry = evaluate_and_reserve(store, reversed_, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                 expected_snapshot_version=v)
    store.close()
    assert retry.blocked and retry.block_reason == "proposal_id reused with a different payload"
    pytest.fail(
        "FINDING P2-3: proposal_hash() canonicalises JSON keys but not leg order; the "
        "same logical proposal retried with its legs in a different order is treated as "
        "a payload conflict and can never be replayed"
    )


def test_FINDING_mutation_scan_scope_excludes_the_spike_module():
    """The builder's architectural scan covers backend/opaca only. The repository
    still ships a runnable order-submitting script."""
    spike = REPO / "spike" / "spike.py"
    if not spike.exists():
        pytest.skip("spike module absent from this checkout")
    tree = ast.parse(spike.read_text(encoding="utf-8"))
    mutators = sorted(
        {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr in {"submit_order", "cancel_order_by_id", "close_position"}
        }
    )
    assert mutators, "probe assumption"
    pytest.fail(
        f"FINDING P2-4: {spike.relative_to(REPO)} calls {mutators} on a live paper "
        "TradingClient built from environment credentials. Pre-existing and outside "
        "backend/opaca, but the 'no broker execution exists' claim is scoped to the "
        "package, not the repository"
    )


def test_FINDING_test_gate_is_not_hermetic():
    """backend/tests/helpers.py resolves REPO_ROOT/spike/evidence; a backend-only
    checkout fails collection."""
    helpers = REPO / "backend" / "tests" / "helpers.py"
    text = helpers.read_text(encoding="utf-8")
    assert 'parents[2]' in text and 'spike' in text
    pytest.fail(
        "FINDING P2-5 (carried over from treasury-core): the backend test suite reads "
        "REPO_ROOT/spike/evidence at import time, so backend/ is not independently "
        "testable"
    )


def test_sqlite_timeout_policy_is_explicit_and_bounded(tmp_path):
    from opaca.persistence.store import SQLiteStore

    store = SQLiteStore(tmp_path / "t.sqlite", timeout=2.5)
    assert store.timeout == 2.5
    assert int(store._conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 2500
    writer = store.connect_writer()
    assert int(writer.execute("PRAGMA busy_timeout").fetchone()[0]) == 2500
    writer.close()
    store.close()


def test_db_files_are_confined_to_the_given_path(tmp_path):
    from opaca.persistence.store import SQLiteStore

    store = SQLiteStore(tmp_path / "x.sqlite")
    store.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert all(n.startswith("x.sqlite") for n in names), names


def test_docs_match_the_implemented_state_machine():
    doc = (REPO / "docs" / "reconciliation-state.md").read_text(encoding="utf-8")
    from opaca.persistence.types import ReconciliationStatus

    for status in ReconciliationStatus:
        assert status.value in doc, status
    execution_doc = REPO / "docs" / "paper-execution.md"
    if execution_doc.exists():
        # Phase 3: execution exists and must be documented as paper-only
        text = execution_doc.read_text(encoding="utf-8")
        assert "paper" in text.lower()
        from opaca.persistence.types import ExecutionState

        for state in ExecutionState:
            assert state.value in text, state
    else:
        assert "Broker execution is NOT implemented" in doc


def test_audit_records_one_event_per_semantic_outcome(tmp_path):
    """Semantic, not a magic count: each proposal gets exactly one evaluation
    event and exactly one terminal decision event, and no detail blob is unbounded."""
    from opaca.persistence.types import AuditEventType

    store, v = reconciled_store(tmp_path, qty="100")
    p = make_proposal("a", [make_order("a", 0, "SGOV", Side.SELL, "1", SGOV)])
    evaluate_and_reserve(
        store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES, expected_snapshot_version=v
    )
    events = store.list_audit(proposal_id="a")
    kinds = [e.event_type for e in events]
    assert kinds.count(AuditEventType.PROPOSAL_EVALUATED) == 1
    terminal = {
        AuditEventType.RESERVATION_CREATED,
        AuditEventType.APPROVAL_REQUIRED,
        AuditEventType.POLICY_REJECTED,
    }
    assert sum(1 for k in kinds if k in terminal) == 1
    assert all(len(e.detail) < 4000 for e in events)
    assert all(e.snapshot_version == v for e in events)
    store.close()


def test_audit_is_not_duplicated_by_an_idempotent_replay(tmp_path):
    from opaca.persistence.types import AuditEventType

    store, v = reconciled_store(tmp_path, qty="100")
    p = make_proposal("a", [make_order("a", 0, "SGOV", Side.SELL, "1", SGOV)])
    for _ in range(3):
        evaluate_and_reserve(
            store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES, expected_snapshot_version=v
        )
    kinds = [e.event_type for e in store.list_audit(proposal_id="a")]
    assert kinds.count(AuditEventType.PROPOSAL_EVALUATED) == 1
    assert kinds.count(AuditEventType.RESERVATION_CREATED) == 1
    assert kinds.count(AuditEventType.IDEMPOTENT_REPLAY) == 2
    store.close()
