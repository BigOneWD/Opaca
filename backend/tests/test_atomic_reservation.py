"""Atomic reservation: concurrency, idempotency, stale snapshot, rollback."""

from __future__ import annotations

import threading
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from opaca.domain.models import AuthorityResult, Proposal, Side
from opaca.orchestration.reserve import OrchestrationResult, evaluate_and_reserve
from opaca.persistence.codec import dump_datetime
from opaca.persistence.store import SQLiteStore
from opaca.persistence.types import AuditEventType, ReconciliationStatus
from opaca.reconciliation.service import reconcile

from tests.helpers import DEFAULT_NOW, DEFAULT_PRICES, make_order, make_proposal
from tests.state_helpers import order_payload, paper_gateway, position_payload, temp_store


def _reconcile_with_position(store: SQLiteStore, qty: str = "100") -> int:
    recon = reconcile(
        store,
        paper_gateway(positions=(position_payload(qty=qty),)),
        now=DEFAULT_NOW,
    )
    assert recon.status is ReconciliationStatus.RECONCILED
    assert recon.snapshot is not None
    return recon.snapshot.version


class TestIdempotency:
    def test_e_same_proposal_processed_twice_is_idempotent(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        version = _reconcile_with_position(store)
        proposal = make_proposal(
            "same-prop",
            [make_order("same-prop", 0, "SGOV", Side.SELL, "10", DEFAULT_PRICES["SGOV"])],
        )
        first = evaluate_and_reserve(
            store,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=version,
        )
        count = store.count_reservations("same-prop")
        second = evaluate_and_reserve(
            store,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=version,
        )
        assert first.authority_result is AuthorityResult.AUTO
        assert first.reserved is True
        assert second.idempotent_replay is True
        assert second.authority_result is AuthorityResult.AUTO
        assert second.reserved is True
        assert store.count_reservations("same-prop") == count
        assert count > 0
        assert store.get_proposal("same-prop") is not None
        assert second.is_auto is True
        store.close()

    def test_replay_changed_hash_fails_closed(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        version = _reconcile_with_position(store)
        first = make_proposal(
            "hash-prop",
            [make_order("hash-prop", 0, "SGOV", Side.SELL, "10", DEFAULT_PRICES["SGOV"])],
        )
        assert evaluate_and_reserve(
            store,
            first,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=version,
        ).is_auto
        count = store.count_reservations("hash-prop")
        changed = make_proposal(
            "hash-prop",
            [make_order("hash-prop", 0, "SGOV", Side.SELL, "11", DEFAULT_PRICES["SGOV"])],
        )
        outcome = evaluate_and_reserve(
            store,
            changed,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=version,
        )
        assert outcome.blocked is True
        assert outcome.is_auto is False
        assert outcome.idempotent_replay is False
        assert store.count_reservations("hash-prop") == count
        denied = store.list_audit(
            event_type=AuditEventType.RESERVATION_DENIED, proposal_id="hash-prop"
        )
        assert denied
        store.close()


class TestStaleAndRollback:
    def test_g_stale_snapshot_cannot_reserve(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        version = _reconcile_with_position(store)
        reconcile(store, paper_gateway(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
        proposal = make_proposal(
            "stale-prop",
            [make_order("stale-prop", 0, "SGOV", Side.SELL, "10", DEFAULT_PRICES["SGOV"])],
        )
        outcome = evaluate_and_reserve(
            store,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=version,
        )
        assert outcome.blocked is True
        assert outcome.reserved is False
        assert outcome.authority_result is not AuthorityResult.AUTO
        assert store.get_proposal("stale-prop") is None
        store.close()

    def test_h_transaction_rollback_leaves_no_partial_reservation(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        version = _reconcile_with_position(store)
        proposal = make_proposal(
            "boom-prop",
            [make_order("boom-prop", 0, "SGOV", Side.SELL, "10", DEFAULT_PRICES["SGOV"])],
        )
        original = store.persist_reservations

        def exploding(**kwargs: object) -> None:
            original(**kwargs)  # type: ignore[arg-type]
            raise RuntimeError("injected failure")

        store.persist_reservations = exploding  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            evaluate_and_reserve(
                store,
                proposal,
                now=DEFAULT_NOW,
                prices=DEFAULT_PRICES,
                expected_snapshot_version=version,
            )
        assert store.get_proposal("boom-prop") is None
        assert store.count_reservations("boom-prop") == 0
        store.close()


class TestConcurrency:
    def test_f_simultaneous_sells_cannot_oversell(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        version = _reconcile_with_position(store, qty="100")
        path = store.path
        store.close()

        results: list[OrchestrationResult | None] = [None, None]
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def worker(index: int, proposal_id: str) -> None:
            local = SQLiteStore(path)
            try:
                proposal = make_proposal(
                    proposal_id,
                    [make_order(proposal_id, 0, "SGOV", Side.SELL, "60", DEFAULT_PRICES["SGOV"])],
                )
                barrier.wait(timeout=5)
                results[index] = evaluate_and_reserve(
                    local,
                    proposal,
                    now=DEFAULT_NOW,
                    prices=DEFAULT_PRICES,
                    expected_snapshot_version=version,
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                local.close()

        threads = [
            threading.Thread(target=worker, args=(0, "concurrent-a")),
            threading.Thread(target=worker, args=(1, "concurrent-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert not errors
        outcomes = [item for item in results if item is not None]
        assert len(outcomes) == 2
        autos = [item for item in outcomes if item.is_auto]
        denied = [item for item in outcomes if not item.reserved]
        assert len(autos) == 1
        assert len(denied) == 1
        assert not (autos[0].reserved and denied[0].reserved)


class TestReplaySafety:
    def _auto_sell(
        self, store: SQLiteStore, version: int, pid: str = "live"
    ) -> tuple[Proposal, OrchestrationResult]:
        proposal = make_proposal(
            pid,
            [make_order(pid, 0, "SGOV", Side.SELL, "10", DEFAULT_PRICES["SGOV"])],
        )
        outcome = evaluate_and_reserve(
            store,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=version,
        )
        assert outcome.is_auto
        return proposal, outcome

    def test_safe_replay_while_fresh_reconciled(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        version = _reconcile_with_position(store)
        proposal, _first = self._auto_sell(store, version)
        before = (len(store.load_autonomous_history()), store.count_reservations("live"))
        replay = evaluate_and_reserve(
            store,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=version,
        )
        assert replay.idempotent_replay is True
        assert replay.is_auto is True
        assert replay.reserved is True
        assert (len(store.load_autonomous_history()), store.count_reservations("live")) == before
        store.close()

    def test_replay_kill_switch_not_executable(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        version = _reconcile_with_position(store)
        proposal, _ = self._auto_sell(store, version)
        store.set_kill_switch(True, now=DEFAULT_NOW)
        replay = evaluate_and_reserve(
            store,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=version,
        )
        assert replay.idempotent_replay is True
        assert replay.is_auto is False
        assert replay.blocked is True
        assert replay.reserved is False
        assert store.count_reservations("live") > 0
        assert store.list_audit(event_type=AuditEventType.RESERVATION_DENIED, proposal_id="live")
        store.close()

    def test_replay_drift_not_executable(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        version = _reconcile_with_position(store)
        proposal, _ = self._auto_sell(store, version)
        recon = reconcile(
            store,
            paper_gateway(
                positions=(position_payload(qty="100"),),
                open_orders=(order_payload("ghost", status="new"),),
            ),
            now=DEFAULT_NOW,
        )
        assert recon.status is ReconciliationStatus.DRIFT_DETECTED
        assert recon.snapshot is not None
        replay = evaluate_and_reserve(
            store,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=recon.snapshot.version,
        )
        assert replay.idempotent_replay is True
        assert replay.is_auto is False
        assert replay.blocked is True
        store.close()

    def test_replay_unknown_not_executable(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        version = _reconcile_with_position(store)
        proposal, _ = self._auto_sell(store, version)
        recon = reconcile(
            store,
            paper_gateway(
                positions=(position_payload(qty="100"),),
                open_orders=(order_payload("weird", status="frobnicated"),),
            ),
            now=DEFAULT_NOW,
        )
        assert recon.status is ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW
        assert recon.snapshot is not None
        replay = evaluate_and_reserve(
            store,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=recon.snapshot.version,
        )
        assert replay.is_auto is False
        assert replay.blocked is True
        store.close()

    def test_replay_stale_snapshot_not_executable(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        version = _reconcile_with_position(store)
        proposal, _ = self._auto_sell(store, version)
        later = DEFAULT_NOW + timedelta(seconds=61)
        replay = evaluate_and_reserve(
            store,
            proposal,
            now=later,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=version,
        )
        assert replay.is_auto is False
        assert replay.blocked is True
        assert store.list_audit(event_type=AuditEventType.STALE_SNAPSHOT)
        store.close()

    def test_replay_wrong_version_not_executable(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        v1 = _reconcile_with_position(store)
        proposal, _ = self._auto_sell(store, v1)
        reconcile(store, paper_gateway(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
        v2 = store.latest_snapshot()
        assert v2 is not None and v2.version != v1
        replay = evaluate_and_reserve(
            store,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=v1,
        )
        assert replay.is_auto is False
        assert replay.blocked is True
        assert replay.idempotent_replay is True
        assert store.list_audit(event_type=AuditEventType.STALE_SNAPSHOT)
        store.close()


class TestSnapshotFreshness:
    def test_omitted_version_cannot_reserve(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        _reconcile_with_position(store)
        proposal = make_proposal(
            "nover",
            [make_order("nover", 0, "SGOV", Side.SELL, "10", DEFAULT_PRICES["SGOV"])],
        )
        outcome = evaluate_and_reserve(store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES)
        assert outcome.blocked is True
        assert outcome.is_auto is False
        assert outcome.reserved is False
        assert store.get_proposal("nover") is None
        store.close()

    def test_aged_snapshot_cannot_reserve(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        version = _reconcile_with_position(store)
        later = DEFAULT_NOW + timedelta(days=30)
        proposal = make_proposal(
            "ancient",
            [make_order("ancient", 0, "SGOV", Side.SELL, "10", DEFAULT_PRICES["SGOV"])],
        )
        outcome = evaluate_and_reserve(
            store,
            proposal,
            now=later,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=version,
        )
        assert outcome.is_auto is False
        assert outcome.blocked is True
        assert store.get_proposal("ancient") is None
        store.close()

    def test_exact_age_boundary_is_fresh(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        version = _reconcile_with_position(store)
        boundary = DEFAULT_NOW + timedelta(seconds=60)
        proposal = make_proposal(
            "boundary",
            [make_order("boundary", 0, "SGOV", Side.SELL, "10", DEFAULT_PRICES["SGOV"])],
        )
        outcome = evaluate_and_reserve(
            store,
            proposal,
            now=boundary,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=version,
        )
        assert outcome.is_auto is True
        store.close()

    def test_future_captured_at_fails_closed(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        version = _reconcile_with_position(store)
        earlier = DEFAULT_NOW - timedelta(seconds=1)
        proposal = make_proposal(
            "future-snap",
            [make_order("future-snap", 0, "SGOV", Side.SELL, "10", DEFAULT_PRICES["SGOV"])],
        )
        outcome = evaluate_and_reserve(
            store,
            proposal,
            now=earlier,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=version,
        )
        assert outcome.blocked is True
        assert outcome.is_auto is False
        assert store.get_proposal("future-snap") is None
        store.close()


class TestApprovalRequired:
    def test_approval_required_does_not_reserve_auto_capacity(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        recon = reconcile(store, paper_gateway(), now=DEFAULT_NOW)
        assert recon.snapshot is not None
        store._conn.execute(
            "INSERT INTO autonomous_executions(proposal_id, timestamp, notional) VALUES (?, ?, ?)",
            ("prior-auto", dump_datetime(DEFAULT_NOW), "50000"),
        )
        proposal = make_proposal(
            "escalate-buy",
            [make_order("escalate-buy", 0, "SGOV", Side.BUY, "1", Decimal("100.00"))],
        )
        prices = {"SGOV": Decimal("100.00"), "BIL": Decimal("92.00"), "SHV": Decimal("110.00")}
        outcome = evaluate_and_reserve(
            store,
            proposal,
            now=DEFAULT_NOW,
            prices=prices,
            expected_snapshot_version=recon.snapshot.version,
        )
        assert outcome.authority_result is AuthorityResult.APPROVAL_REQUIRED
        assert outcome.reserved is False
        record = store.get_proposal("escalate-buy")
        assert record is not None
        assert record.expires_at is not None
        assert store.count_reservations("escalate-buy") == 0
        assert outcome.approval_currently_valid(DEFAULT_NOW) is True
        assert record.is_currently_valid_approval(DEFAULT_NOW) is True
        assert record.is_currently_valid_approval(record.expires_at) is False
        assert record.is_currently_valid_approval(record.expires_at + timedelta(seconds=1)) is False
        store.close()

    def test_approval_expiry_before_exact_after(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        recon = reconcile(store, paper_gateway(), now=DEFAULT_NOW)
        assert recon.snapshot is not None
        store._conn.execute(
            "INSERT INTO autonomous_executions(proposal_id, timestamp, notional) VALUES (?, ?, ?)",
            ("prior-auto", dump_datetime(DEFAULT_NOW), "50000"),
        )
        proposal = make_proposal(
            "expiry-buy",
            [make_order("expiry-buy", 0, "SGOV", Side.BUY, "1", Decimal("100.00"))],
        )
        prices = {"SGOV": Decimal("100.00"), "BIL": Decimal("92.00"), "SHV": Decimal("110.00")}
        first = evaluate_and_reserve(
            store,
            proposal,
            now=DEFAULT_NOW,
            prices=prices,
            expected_snapshot_version=recon.snapshot.version,
        )
        assert first.authority_result is AuthorityResult.APPROVAL_REQUIRED
        assert first.expires_at is not None
        before = DEFAULT_NOW + timedelta(seconds=30)
        replay_before = evaluate_and_reserve(
            store,
            proposal,
            now=before,
            prices=prices,
            expected_snapshot_version=recon.snapshot.version,
        )
        assert replay_before.idempotent_replay is True
        assert replay_before.is_auto is False
        assert replay_before.approval_currently_valid(before) is True
        assert store.count_reservations("expiry-buy") == 0

        later = first.expires_at
        recon_later = reconcile(store, paper_gateway(), now=later)
        assert recon_later.snapshot is not None
        exact = evaluate_and_reserve(
            store,
            proposal,
            now=later,
            prices=prices,
            expected_snapshot_version=recon_later.snapshot.version,
        )
        assert exact.idempotent_replay is True
        assert exact.is_auto is False
        assert exact.approval_currently_valid(later) is False
        assert exact.blocked is True
        assert exact.block_reason == "approval expired"
        after = later + timedelta(seconds=1)
        recon_after = reconcile(store, paper_gateway(), now=after)
        assert recon_after.snapshot is not None
        expired = evaluate_and_reserve(
            store,
            proposal,
            now=after,
            prices=prices,
            expected_snapshot_version=recon_after.snapshot.version,
        )
        assert expired.approval_currently_valid(after) is False
        assert expired.blocked is True
        assert store.count_reservations("expiry-buy") == 0
        store.close()
