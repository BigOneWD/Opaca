"""Fail-closed paths: none may silently become AUTO."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from opaca.broker.gateway import FakeAlpacaGateway
from opaca.domain.models import AuthorityResult, OrderState, Side
from opaca.domain.money import MoneyError
from opaca.orchestration.context import build_policy_context
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.store import SqliteBusyError, SQLiteStore
from opaca.persistence.types import ReconciliationStatus, UnknownOrderRecord
from opaca.reconciliation.service import reconcile

from tests.helpers import DEFAULT_NOW, make_order, make_proposal
from tests.state_helpers import (
    PHASE1_ASSETS,
    paper_gateway,
    temp_store,
)


def _auto_forbidden(outcome_authority: AuthorityResult | None, reserved: bool) -> None:
    assert outcome_authority is not AuthorityResult.AUTO
    assert reserved is False


class TestBrokerFailures:
    def test_broker_unavailable(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        result = reconcile(store, paper_gateway(unavailable=True), now=DEFAULT_NOW)
        assert result.status is ReconciliationStatus.BROKER_UNAVAILABLE
        store.close()

    def test_malformed_account_response(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        gateway = FakeAlpacaGateway(account={"buying_power": "400000"}, assets=PHASE1_ASSETS)
        result = reconcile(store, gateway, now=DEFAULT_NOW)
        assert result.status is ReconciliationStatus.INVALID_BROKER_STATE
        store.close()

    def test_malformed_position(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        gateway = paper_gateway(
            positions=(
                {
                    "symbol": "SGOV",
                    "qty": "not-a-number",
                    "qty_available": "1",
                    "market_value": "1",
                },
            )
        )
        result = reconcile(store, gateway, now=DEFAULT_NOW)
        assert result.status is ReconciliationStatus.INVALID_BROKER_STATE
        store.close()

    def test_malformed_unresolved_order(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        gateway = paper_gateway(
            open_orders=(
                {"client_order_id": "x", "symbol": "SGOV", "side": "sideways", "status": "new"},
            )
        )
        result = reconcile(store, gateway, now=DEFAULT_NOW)
        assert result.status is ReconciliationStatus.INVALID_BROKER_STATE
        store.close()


class TestPriceAndCalendar:
    def test_j_malformed_positive_price_boundary_fails_closed(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        recon = reconcile(store, paper_gateway(), now=DEFAULT_NOW)
        assert recon.status is ReconciliationStatus.RECONCILED
        with pytest.raises(MoneyError):
            build_policy_context(store, now=DEFAULT_NOW, prices={"SGOV": Decimal("0")})
        with pytest.raises(MoneyError):
            build_policy_context(store, now=DEFAULT_NOW, prices={"SGOV": Decimal("-1")})
        proposal = make_proposal(
            "price-missing",
            [make_order("price-missing", 0, "SGOV", Side.BUY, "1", Decimal("100.00"))],
        )
        outcome = evaluate_and_reserve(
            store,
            proposal,
            now=DEFAULT_NOW,
            prices={"BIL": Decimal("92.00")},
        )
        _auto_forbidden(outcome.authority_result, outcome.reserved)
        store.close()

    def test_unsupported_calendar_date_fails_closed(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        saturday = datetime(2026, 9, 5, 14, 30, tzinfo=DEFAULT_NOW.tzinfo)
        recon = reconcile(store, paper_gateway(), now=saturday)
        assert recon.snapshot is not None
        proposal = make_proposal(
            "weekend",
            [make_order("weekend", 0, "SGOV", Side.BUY, "1", Decimal("100.00"))],
        )
        prices = {"SGOV": Decimal("100.00"), "BIL": Decimal("92.00"), "SHV": Decimal("110.00")}
        outcome = evaluate_and_reserve(
            store,
            proposal,
            now=saturday,
            prices=prices,
            expected_snapshot_version=recon.snapshot.version,
        )
        assert outcome.authority_result is AuthorityResult.REJECT
        assert outcome.reserved is False
        store.close()


class TestSqliteBusy:
    def test_sqlite_locked_busy_fails_closed(self, tmp_path: Path) -> None:
        path = tmp_path / "busy.sqlite"
        holder = SQLiteStore(path, timeout=0.2)
        contender = SQLiteStore(path, timeout=0.2)
        holder._conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(SqliteBusyError), contender.begin_immediate():
            pass
        holder._conn.execute("ROLLBACK")
        holder.close()
        contender.close()


class TestUnknownLookup:
    def test_unknown_lookup_not_found_requires_review(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        reconcile(store, paper_gateway(), now=DEFAULT_NOW)
        with store.begin_immediate() as conn:
            store.upsert_unknown_order(
                UnknownOrderRecord(
                    client_order_id="missing-at-broker",
                    proposal_id="u1",
                    symbol="SGOV",
                    side="SELL",
                    quantity=Decimal("1"),
                    filled_quantity=Decimal("0"),
                    state=OrderState.UNKNOWN.value,
                    last_lookup_at=None,
                    created_at=DEFAULT_NOW,
                ),
                conn=conn,
            )
        result = reconcile(store, paper_gateway(), now=DEFAULT_NOW)
        assert result.status is ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW
        unknown = store.load_unknown_orders()
        assert unknown[0].state == OrderState.UNKNOWN_REQUIRES_REVIEW.value
        store.close()
