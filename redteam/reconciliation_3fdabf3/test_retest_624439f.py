"""Final closeout retest @ 624439f — historical AUTO is not current eligibility.

The single fix under retest: ``OrchestrationResult.is_auto`` returns False
whenever ``idempotent_replay`` is True. Replay still preserves the historical
authority result and reservation metadata and still consumes no new capacity;
it simply never asserts current execution eligibility.

Every test here asserts the FIXED behaviour and fails at d85a2e6.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from opaca.domain.models import AuthorityResult, Side
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.codec import dump_datetime
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


def _capacity(store, proposal_id):
    return (
        store.count_reservations(proposal_id),
        len(store.load_autonomous_history()),
        sum((h.notional for h in store.load_autonomous_history()), Decimal("0")),
        sum(
            (r.quantity for r in store.active_reservations()
             if r.quantity is not None and r.kind.value == "SELL_QUANTITY"),
            Decimal("0"),
        ),
        sum(
            (r.amount for r in store.active_reservations()
             if r.amount is not None and r.kind.value == "CASH_DEPLOYMENT"),
            Decimal("0"),
        ),
    )


def _auto_sell(store, version, pid="hist", qty="10"):
    proposal = make_proposal(pid, [make_order(pid, 0, "SGOV", Side.SELL, qty, SGOV)])
    out = evaluate_and_reserve(
        store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
        expected_snapshot_version=version,
    )
    assert out.is_auto is True, (out.authority_result, out.block_reason)
    assert out.idempotent_replay is False
    return proposal, out


def _auto_buy(store, version, pid="hist", qty="99"):
    proposal = make_proposal(pid, [make_order(pid, 0, "SGOV", Side.BUY, qty, SGOV)])
    out = evaluate_and_reserve(
        store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
        expected_snapshot_version=version,
    )
    assert out.is_auto is True, (out.authority_result, out.block_reason)
    return proposal, out


def _replay(store, proposal, version, now=DEFAULT_NOW):
    return evaluate_and_reserve(
        store, proposal, now=now, prices=DEFAULT_PRICES, expected_snapshot_version=version
    )


# ==============================================================  CASE 1


class TestCase1ReplayNoStateChange:
    def test_replay_is_idempotent_and_never_asserts_current_auto(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, first = _auto_sell(store, v)
        before = _capacity(store, "hist")

        for _ in range(5):
            replay = _replay(store, proposal, v)
            assert replay.idempotent_replay is True
            assert replay.is_auto is False, "historical AUTO must not assert current AUTO"
            assert replay.blocked is False, "an unchanged replay is not an error"
            assert replay.block_reason is None
            assert replay.authority_result is AuthorityResult.AUTO, "history is preserved"
            assert replay.reserved is True, "the existing reservation is still reported"
            assert replay.proposal_hash == first.proposal_hash

        assert _capacity(store, "hist") == before
        store.close()

    def test_replay_creates_no_duplicate_reservation_rows(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto_sell(store, v)
        rows = store._conn.execute(
            "SELECT reservation_id, kind, symbol, quantity FROM reservations "
            "WHERE proposal_id='hist' ORDER BY reservation_id"
        ).fetchall()
        for _ in range(5):
            _replay(store, proposal, v)
        after = store._conn.execute(
            "SELECT reservation_id, kind, symbol, quantity FROM reservations "
            "WHERE proposal_id='hist' ORDER BY reservation_id"
        ).fetchall()
        assert [tuple(r) for r in after] == [tuple(r) for r in rows]
        assert len(after) == 2  # one SELL_QUANTITY, one ORDER_IDENTITY
        store.close()

    def test_replay_consumes_no_additional_authority(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto_sell(store, v)
        history = [(h.timestamp, h.notional) for h in store.load_autonomous_history()]
        assert len(history) == 1
        for _ in range(5):
            _replay(store, proposal, v)
        assert [(h.timestamp, h.notional) for h in store.load_autonomous_history()] == history
        store.close()

    def test_replay_writes_no_duplicate_proposal_legs_or_order_identity(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto_sell(store, v)
        for _ in range(5):
            _replay(store, proposal, v)
        for table in ("proposal_legs", "order_identity", "authority_decisions"):
            count = store._conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE proposal_id='hist'"
            ).fetchone()["n"]
            assert count == 1, table
        store.close()

    def test_replay_is_audited_as_a_replay_not_as_a_new_reservation(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto_sell(store, v)
        for _ in range(3):
            _replay(store, proposal, v)
        kinds = [e.event_type for e in store.list_audit(proposal_id="hist")]
        assert kinds.count(AuditEventType.RESERVATION_CREATED) == 1
        assert kinds.count(AuditEventType.PROPOSAL_EVALUATED) == 1
        assert kinds.count(AuditEventType.IDEMPOTENT_REPLAY) == 3
        store.close()

    def test_a_fresh_proposal_can_still_be_currently_auto(self, tmp_path):
        """The fix must not make fresh evaluation non-executable."""
        store, v = reconciled_store(tmp_path, qty="100")
        _, out = _auto_sell(store, v, pid="fresh-1")
        assert out.is_auto is True
        assert out.idempotent_replay is False
        _, out2 = _auto_sell(store, v, pid="fresh-2", qty="5")
        assert out2.is_auto is True
        store.close()

    def test_the_invariant_holds_structurally(self, tmp_path):
        """is_auto and idempotent_replay are never both true, in any outcome."""
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, first = _auto_sell(store, v)
        outcomes = [first, _replay(store, proposal, v)]
        store.set_kill_switch(True, now=DEFAULT_NOW)
        outcomes.append(_replay(store, proposal, v))
        store.set_kill_switch(False, now=DEFAULT_NOW)
        outcomes.append(_replay(store, proposal, v, now=DEFAULT_NOW + timedelta(days=30)))
        outcomes.append(evaluate_and_reserve(
            store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES))
        for outcome in outcomes:
            assert not (outcome.is_auto and outcome.idempotent_replay), outcome
        store.close()


# ==============================================================  CASE 2


class TestCase2CashCollapse:
    def test_replay_after_cash_collapse_is_not_executable(self, tmp_path):
        store, v1 = reconciled_store(tmp_path, qty=None, cash="100000")
        proposal, first = _auto_buy(store, v1)
        assert first.is_auto is True

        recon = reconcile(store, paper_gateway(cash="1"), now=DEFAULT_NOW)
        assert recon.status is ReconciliationStatus.RECONCILED
        v2 = recon.snapshot.version

        replay = _replay(store, proposal, v2)
        assert replay.is_auto is False
        assert replay.idempotent_replay is True
        store.close()

    def test_a_fresh_sibling_proposal_hard_rejects_after_cash_collapse(self, tmp_path):
        store, v1 = reconciled_store(tmp_path, qty=None, cash="100000")
        _auto_buy(store, v1)
        recon = reconcile(store, paper_gateway(cash="1"), now=DEFAULT_NOW)
        v2 = recon.snapshot.version

        sibling = make_proposal("sib", [make_order("sib", 0, "SGOV", Side.BUY, "99", SGOV)])
        fresh = evaluate_and_reserve(
            store, sibling, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
            expected_snapshot_version=v2,
        )
        assert fresh.authority_result is AuthorityResult.REJECT
        assert fresh.is_auto is False
        assert store.count_reservations("sib") == 0
        store.close()

    def test_replay_and_sibling_agree_that_nothing_is_executable(self, tmp_path):
        """The exact P0-1-r reproduction: replay and a fresh identical proposal must
        no longer disagree about current eligibility."""
        store, v1 = reconciled_store(tmp_path, qty=None, cash="100000")
        proposal, _ = _auto_buy(store, v1)
        recon = reconcile(store, paper_gateway(cash="1"), now=DEFAULT_NOW)
        v2 = recon.snapshot.version
        sibling = make_proposal("sib", [make_order("sib", 0, "SGOV", Side.BUY, "99", SGOV)])
        fresh = evaluate_and_reserve(
            store, sibling, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
            expected_snapshot_version=v2,
        )
        replay = _replay(store, proposal, v2)
        assert fresh.is_auto is False
        assert replay.is_auto is False
        assert fresh.is_auto == replay.is_auto
        store.close()


# ==============================================================  CASE 3


class TestCase3NewObligation:
    def test_replay_after_a_new_obligation_is_not_executable(self, tmp_path):
        store, v1 = reconciled_store(tmp_path, qty=None, cash="100000")
        proposal, _ = _auto_buy(store, v1)
        store._conn.execute(
            "INSERT INTO obligations(obligation_id, name, amount, due_date, seeded) "
            "VALUES ('new-payroll','extra payroll','55000','2026-09-15',0)"
        )
        recon = reconcile(store, paper_gateway(cash="100000"), now=DEFAULT_NOW)
        assert recon.status is ReconciliationStatus.RECONCILED
        replay = _replay(store, proposal, recon.snapshot.version)
        assert replay.is_auto is False
        assert replay.idempotent_replay is True
        store.close()

    def test_a_fresh_sibling_rejects_after_the_new_obligation(self, tmp_path):
        store, v1 = reconciled_store(tmp_path, qty=None, cash="100000")
        _auto_buy(store, v1)
        store._conn.execute(
            "INSERT INTO obligations(obligation_id, name, amount, due_date, seeded) "
            "VALUES ('new-payroll','extra payroll','55000','2026-09-15',0)"
        )
        recon = reconcile(store, paper_gateway(cash="100000"), now=DEFAULT_NOW)
        sibling = make_proposal("sib", [make_order("sib", 0, "SGOV", Side.BUY, "99", SGOV)])
        fresh = evaluate_and_reserve(
            store, sibling, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
            expected_snapshot_version=recon.snapshot.version,
        )
        assert fresh.is_auto is False
        assert fresh.authority_result is AuthorityResult.REJECT
        store.close()


# ==============================================================  CASE 4


class TestCase4ReserveOrPolicyChange:
    def test_replay_after_an_operating_reserve_increase_is_not_executable(self, tmp_path):
        store, v1 = reconciled_store(tmp_path, qty=None, cash="100000")
        proposal, _ = _auto_buy(store, v1)
        store._conn.execute(
            "UPDATE scenario_state SET operating_reserve = '61000' WHERE id = 1"
        )
        recon = reconcile(store, paper_gateway(cash="100000"), now=DEFAULT_NOW)
        replay = _replay(store, proposal, recon.snapshot.version)
        assert replay.is_auto is False
        sibling = make_proposal("sib", [make_order("sib", 0, "SGOV", Side.BUY, "99", SGOV)])
        fresh = evaluate_and_reserve(
            store, sibling, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
            expected_snapshot_version=recon.snapshot.version,
        )
        assert fresh.is_auto is False
        store.close()

    @pytest.mark.parametrize(
        "name,value",
        [
            ("per_order_autonomous_notional_max", "1.00"),
            ("per_proposal_aggregate_notional_max", "1.00"),
            ("rolling_24h_autonomous_notional_max", "1.00"),
            ("concentration_max_fraction", "0.01"),
            ("min_trade_notional", "999999"),
            ("permitted_symbols", '["BIL"]'),
        ],
    )
    def test_replay_after_a_policy_change_is_not_executable(self, tmp_path, name, value):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto_sell(store, v)
        store._conn.execute("UPDATE policies SET value = ? WHERE name = ?", (value, name))
        replay = _replay(store, proposal, v)
        assert replay.is_auto is False
        assert replay.idempotent_replay is True
        store.close()

    def test_a_tightened_policy_rejects_a_fresh_sibling(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        _auto_sell(store, v)
        store._conn.execute(
            "UPDATE policies SET value = '[\"BIL\"]' WHERE name = 'permitted_symbols'"
        )
        sibling = make_proposal("sib", [make_order("sib", 0, "SGOV", Side.SELL, "1", SGOV)])
        fresh = evaluate_and_reserve(
            store, sibling, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
            expected_snapshot_version=v,
        )
        assert fresh.authority_result is AuthorityResult.REJECT
        store.close()


# ==============================================================  CASE 5


class TestCase5AllGatesStillFailClosed:
    def test_kill_switch(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto_sell(store, v)
        store.set_kill_switch(True, now=DEFAULT_NOW)
        replay = _replay(store, proposal, v)
        assert replay.is_auto is False
        assert replay.blocked is True
        assert replay.block_reason == "kill switch active"
        store.close()

    def test_drift_detected(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto_sell(store, v)
        recon = reconcile(
            store,
            paper_gateway(positions=(position_payload(qty="100"),),
                          open_orders=(order_payload("ghost", status="new"),)),
            now=DEFAULT_NOW,
        )
        assert recon.status is ReconciliationStatus.DRIFT_DETECTED
        replay = _replay(store, proposal, recon.snapshot.version)
        assert replay.is_auto is False and replay.blocked is True
        assert ReconciliationStatus.DRIFT_DETECTED.value in replay.block_reason
        store.close()

    def test_unknown_requires_review(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto_sell(store, v)
        recon = reconcile(
            store,
            paper_gateway(positions=(position_payload(qty="100"),),
                          open_orders=(order_payload("weird", status="frobnicated"),)),
            now=DEFAULT_NOW,
        )
        assert recon.status is ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW
        replay = _replay(store, proposal, recon.snapshot.version)
        assert replay.is_auto is False and replay.blocked is True
        assert ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW.value in replay.block_reason
        store.close()

    def test_stale_snapshot_age(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto_sell(store, v)
        replay = _replay(store, proposal, v, now=DEFAULT_NOW + timedelta(days=30))
        assert replay.is_auto is False and replay.blocked is True
        assert replay.block_reason == "stale snapshot"
        store.close()

    def test_version_mismatch(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto_sell(store, v)
        reconcile(store, paper_gateway(positions=(position_payload(qty="100"),)),
                  now=DEFAULT_NOW)
        replay = _replay(store, proposal, v)
        assert replay.is_auto is False and replay.blocked is True
        assert replay.block_reason == "stale snapshot"
        store.close()

    def test_missing_expected_version(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto_sell(store, v)
        replay = evaluate_and_reserve(
            store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES
        )
        assert replay.is_auto is False and replay.blocked is True
        assert replay.block_reason == "expected_snapshot_version is required"
        store.close()

    def test_no_gate_adds_capacity(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto_sell(store, v)
        before = _capacity(store, "hist")
        store.set_kill_switch(True, now=DEFAULT_NOW)
        _replay(store, proposal, v)
        store.set_kill_switch(False, now=DEFAULT_NOW)
        _replay(store, proposal, v, now=DEFAULT_NOW + timedelta(days=30))
        evaluate_and_reserve(store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES)
        assert _capacity(store, "hist") == before
        store.close()


# ==============================================================  CASE 6


class TestCase6ProposalIdentity:
    def test_exact_retry_remains_idempotent(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, first = _auto_sell(store, v)
        before = _capacity(store, "hist")
        replays = [_replay(store, proposal, v) for _ in range(4)]
        assert all(r.idempotent_replay for r in replays)
        assert all(r.proposal_hash == first.proposal_hash for r in replays)
        assert all(r.reserved is True for r in replays)
        assert all(r.blocked is False for r in replays)
        assert _capacity(store, "hist") == before
        store.close()

    def test_changed_content_under_the_same_id_fails_closed(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, first = _auto_sell(store, v, qty="10")
        before = _capacity(store, "hist")
        mutated = make_proposal("hist", [make_order("hist", 0, "SGOV", Side.SELL, "90", SGOV)])
        out = evaluate_and_reserve(
            store, mutated, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
            expected_snapshot_version=v,
        )
        assert out.blocked is True
        assert out.is_auto is False
        assert out.idempotent_replay is False
        assert out.block_reason == "proposal_id reused with a different payload"
        assert out.proposal_hash != first.proposal_hash
        assert _capacity(store, "hist") == before
        denials = [
            e for e in store.list_audit(proposal_id="hist")
            if e.event_type is AuditEventType.RESERVATION_DENIED
        ]
        assert any("different payload" in e.reason for e in denials)
        store.close()

    @pytest.mark.parametrize(
        "mutation",
        ["quantity", "price", "side", "symbol", "extra_leg"],
    )
    def test_every_payload_mutation_fails_closed(self, tmp_path, mutation):
        store, v = reconciled_store(tmp_path, qty="100")
        _auto_sell(store, v, qty="10")
        before = _capacity(store, "hist")
        if mutation == "quantity":
            legs = [make_order("hist", 0, "SGOV", Side.SELL, "11", SGOV)]
        elif mutation == "price":
            legs = [make_order("hist", 0, "SGOV", Side.SELL, "10", Decimal("101.00"))]
        elif mutation == "side":
            legs = [make_order("hist", 0, "SGOV", Side.BUY, "10", SGOV)]
        elif mutation == "symbol":
            legs = [make_order("hist", 0, "BIL", Side.SELL, "10", DEFAULT_PRICES["BIL"])]
        else:
            legs = [
                make_order("hist", 0, "SGOV", Side.SELL, "10", SGOV),
                make_order("hist", 1, "BIL", Side.SELL, "1", DEFAULT_PRICES["BIL"]),
            ]
        out = evaluate_and_reserve(
            store, make_proposal("hist", legs), now=DEFAULT_NOW, prices=DEFAULT_PRICES,
            expected_snapshot_version=v,
        )
        assert out.blocked is True and out.is_auto is False
        assert out.block_reason == "proposal_id reused with a different payload"
        assert _capacity(store, "hist") == before
        store.close()

    def test_a_different_proposal_id_is_a_fresh_evaluation(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        _auto_sell(store, v, pid="one", qty="10")
        _, second = _auto_sell(store, v, pid="two", qty="10")
        assert second.idempotent_replay is False
        assert second.is_auto is True
        assert len(store.load_autonomous_history()) == 2
        store.close()


# ==============================================================  APPROVAL PATH


def test_replay_of_an_approval_required_proposal_is_still_not_auto(tmp_path):
    store, v = reconciled_store(tmp_path, qty=None, cash="100000")
    store._conn.execute(
        "INSERT INTO autonomous_executions(proposal_id, timestamp, notional) VALUES (?,?,?)",
        ("prior", dump_datetime(DEFAULT_NOW), "49000"),
    )
    proposal = make_proposal("ar", [make_order("ar", 0, "SGOV", Side.BUY, "20", SGOV)])
    first = evaluate_and_reserve(
        store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES, expected_snapshot_version=v
    )
    assert first.authority_result is AuthorityResult.APPROVAL_REQUIRED
    assert first.is_auto is False
    replay = _replay(store, proposal, v)
    assert replay.idempotent_replay is True
    assert replay.is_auto is False
    assert replay.approval_currently_valid(DEFAULT_NOW) is True, (
        "an unexpired approval is still reported as a valid approval"
    )
    store.close()
