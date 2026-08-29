"""Atomic reservation: concurrency, idempotency, stale snapshot, rollback."""

from __future__ import annotations

import threading
from decimal import Decimal
from pathlib import Path

import pytest
from opaca.domain.models import AuthorityResult, Side
from opaca.orchestration.reserve import OrchestrationResult, evaluate_and_reserve
from opaca.persistence.store import SQLiteStore
from opaca.persistence.types import ReconciliationStatus
from opaca.reconciliation.service import reconcile

from tests.helpers import DEFAULT_NOW, DEFAULT_PRICES, make_order, make_proposal
from tests.state_helpers import paper_gateway, position_payload, temp_store


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


class TestApprovalRequired:
    def test_approval_required_does_not_reserve_auto_capacity(self, tmp_path: Path) -> None:
        from opaca.persistence.codec import dump_datetime

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
        store.close()
