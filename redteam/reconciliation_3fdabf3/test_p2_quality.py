"""P2: architecture / test quality. Non-blocking observations, each demonstrated."""

from __future__ import annotations

import ast
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
import opaca
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


def test_FINDING_reservations_are_never_released(tmp_path):
    """No code path transitions a reservation out of ACTIVE, and nothing expires one."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in ROOT.rglob("*.py"))
    assert "ReservationStatus.RELEASED" not in text
    assert "UPDATE reservations" not in text
    assert "DELETE FROM reservations" not in text
    store, v = reconciled_store(tmp_path, qty="100")
    p = make_proposal("s", [make_order("s", 0, "SGOV", Side.SELL, "10", SGOV)])
    assert evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                expected_snapshot_version=v).is_auto
    assert all(r.status is ReservationStatus.ACTIVE for r in store.active_reservations())
    store.close()
    pytest.fail(
        "FINDING P2-1: reservations are created ACTIVE and never released, expired or "
        "reconciled away. Sell capacity and deployable cash are consumed permanently "
        "for the life of the database"
    )


def test_FINDING_one_auto_sell_locks_out_every_later_buy_of_that_symbol(tmp_path):
    """CHECK-10 treats the never-released SELL reservation as an opposing unresolved
    order, so no BUY of that symbol can ever be AUTO again."""
    store, v = reconciled_store(tmp_path, qty="100", cash="100000")
    sell = make_proposal("s", [make_order("s", 0, "SGOV", Side.SELL, "1", SGOV)])
    assert evaluate_and_reserve(store, sell, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                expected_snapshot_version=v).is_auto
    buy = make_proposal("b", [make_order("b", 0, "SGOV", Side.BUY, "1", SGOV)])
    out = evaluate_and_reserve(store, buy, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                               expected_snapshot_version=v)
    store.close()
    assert out.authority_result is AuthorityResult.REJECT, out.authority_result
    pytest.fail(
        "FINDING P2-2: after a single AUTO sell of SGOV, every later BUY of SGOV is a "
        "hard REJECT (CHECK-10 opposing unresolved order) and stays that way, because "
        "the reservation is never released"
    )


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
    assert "Broker execution is NOT implemented" in doc


def test_audit_verbosity_is_bounded(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    p = make_proposal("a", [make_order("a", 0, "SGOV", Side.SELL, "1", SGOV)])
    evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                         expected_snapshot_version=v)
    events = store.list_audit()
    assert len(events) <= 12, len(events)
    assert max(len(e.detail) for e in events) < 4000
    store.close()
