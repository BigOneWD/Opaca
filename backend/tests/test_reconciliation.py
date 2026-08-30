"""Reconciliation states and required proofs A–D, K, L."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from opaca.domain.models import AuthorityResult, OrderState, SettlementEvent, Side
from opaca.domain.money import round_budget
from opaca.persistence.types import AuditEventType, ReconciliationStatus, UnknownOrderRecord
from opaca.reconciliation.service import reconcile
from opaca.treasury.liquidity import compute_liquidity

from tests.helpers import DEFAULT_NOW, DEFAULT_PRICES, make_order, make_proposal
from tests.state_helpers import (
    b7_cash,
    order_payload,
    paper_gateway,
    position_payload,
    temp_store,
)


class TestCashReconciliation:
    def test_a_99999_99_broker_cash_reconciles(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        result = reconcile(store, paper_gateway(cash="99999.99"), now=DEFAULT_NOW)
        assert result.status is ReconciliationStatus.RECONCILED
        assert result.broker is not None
        assert result.broker.cash == Decimal("99999.99")
        assert result.broker.cash == b7_cash()
        seed = store.get_scenario()
        assert seed is not None
        assert seed.opening_cash == Decimal("99999.99")
        store.close()

    def test_b_4x_buying_power_cannot_affect_investable_funding(self, tmp_path: Path) -> None:
        from opaca.orchestration.reserve import evaluate_and_reserve

        store = temp_store(tmp_path)
        recon = reconcile(store, paper_gateway(cash="100000"), now=DEFAULT_NOW)
        assert recon.status is ReconciliationStatus.RECONCILED
        assert recon.broker is not None
        assert recon.broker.buying_power == Decimal("400000")
        proposal = make_proposal(
            "prop-leverage",
            [make_order("prop-leverage", 0, "SGOV", Side.BUY, "3972", "100.70")],
        )
        prices = {"SGOV": Decimal("100.70"), "BIL": Decimal("92.00"), "SHV": Decimal("110.00")}
        outcome = evaluate_and_reserve(
            store,
            proposal,
            now=DEFAULT_NOW,
            prices=prices,
            expected_snapshot_version=recon.snapshot.version if recon.snapshot else None,
        )
        assert outcome.authority_result is AuthorityResult.REJECT
        assert outcome.reserved is False
        store.close()

    def test_c_unsettled_sale_proceeds_reconcile(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        event = SettlementEvent(
            event_id="b7-leg-0",
            symbol="SGOV",
            trade_date=date(2026, 8, 28),
            settlement_date=date(2026, 8, 31),
            amount=round_budget(Decimal("1.199207531") * Decimal("100.69")),
        )
        store.insert_settlement_event(event, now=DEFAULT_NOW)
        result = reconcile(store, paper_gateway(cash="99999.99"), now=DEFAULT_NOW)
        assert result.status is ReconciliationStatus.RECONCILED
        assert result.broker is not None
        projection = compute_liquidity(
            result.broker,
            obligations=(),
            settlement_events=(event,),
            operating_reserve=Decimal("0"),
            as_of=date(2026, 8, 28),
        )
        assert event.amount == Decimal("120.74")
        assert projection.settled_cash == Decimal("99879.25")
        store.close()

    def test_unsettled_exceeding_cash_is_drift(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        store.insert_settlement_event(
            SettlementEvent(
                event_id="too-big",
                symbol="SGOV",
                trade_date=date(2026, 9, 1),
                settlement_date=date(2026, 9, 2),
                amount=Decimal("200000.00"),
            ),
            now=DEFAULT_NOW,
        )
        result = reconcile(store, paper_gateway(cash="100000"), now=DEFAULT_NOW)
        assert result.status is ReconciliationStatus.DRIFT_DETECTED
        store.close()


class TestReservationsAndDrift:
    def test_d_unresolved_sell_affects_effective_available(self, tmp_path: Path) -> None:
        from opaca.orchestration.reserve import evaluate_and_reserve

        store = temp_store(tmp_path)
        gateway = paper_gateway(positions=(position_payload(qty="100"),))
        recon = reconcile(store, gateway, now=DEFAULT_NOW)
        assert recon.status is ReconciliationStatus.RECONCILED
        first = make_proposal(
            "sell-a",
            [make_order("sell-a", 0, "SGOV", Side.SELL, "60", DEFAULT_PRICES["SGOV"])],
        )
        outcome_a = evaluate_and_reserve(
            store,
            first,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=recon.snapshot.version if recon.snapshot else None,
        )
        assert outcome_a.authority_result is AuthorityResult.AUTO
        assert outcome_a.reserved is True
        second = make_proposal(
            "sell-b",
            [make_order("sell-b", 0, "SGOV", Side.SELL, "60", DEFAULT_PRICES["SGOV"])],
        )
        outcome_b = evaluate_and_reserve(
            store,
            second,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=recon.snapshot.version if recon.snapshot else None,
        )
        assert outcome_b.authority_result is AuthorityResult.REJECT
        assert outcome_b.reserved is False
        store.close()

    def test_external_broker_order_is_drift(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        gateway = paper_gateway(
            positions=(position_payload(qty="100", qty_available="40"),),
            open_orders=(
                {
                    "id": "ext-1",
                    "client_order_id": "external-cli-order",
                    "symbol": "SGOV",
                    "side": "sell",
                    "status": "new",
                    "qty": "60",
                    "filled_qty": "0",
                },
            ),
        )
        result = reconcile(store, gateway, now=DEFAULT_NOW)
        assert result.status is ReconciliationStatus.DRIFT_DETECTED
        store.close()

    def test_position_quantity_change_is_drift(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        first = paper_gateway(positions=(position_payload(qty="100"),))
        assert reconcile(store, first, now=DEFAULT_NOW).status is ReconciliationStatus.RECONCILED
        second = paper_gateway(positions=(position_payload(qty="40"),))
        result = reconcile(store, second, now=DEFAULT_NOW)
        assert result.status is ReconciliationStatus.DRIFT_DETECTED
        events = store.list_audit(event_type=AuditEventType.DRIFT_DETECTED)
        assert events
        store.close()

    def test_k_l_seed_once_cash_change_does_not_reseed(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        first = reconcile(store, paper_gateway(cash="100000"), now=DEFAULT_NOW)
        assert first.status is ReconciliationStatus.RECONCILED
        seed = store.get_scenario()
        assert seed is not None
        assert seed.operating_reserve == Decimal("40000.00")
        again = reconcile(store, paper_gateway(cash="90000"), now=DEFAULT_NOW)
        assert again.status is ReconciliationStatus.RECONCILED
        reseed = store.get_scenario()
        assert reseed is not None
        assert reseed.opening_cash == Decimal("100000")
        assert reseed.operating_reserve == Decimal("40000.00")
        payroll, suppliers = reseed.obligations
        assert payroll.amount == Decimal("24000.00")
        assert suppliers.amount == Decimal("14000.00")
        store.close()


class TestUnknown:
    def test_i_unknown_never_auto_retries(self, tmp_path: Path) -> None:
        from opaca.orchestration.reserve import evaluate_and_reserve
        from opaca.persistence.types import UnknownOrderRecord

        store = temp_store(tmp_path)
        recon = reconcile(store, paper_gateway(), now=DEFAULT_NOW)
        assert recon.status is ReconciliationStatus.RECONCILED
        with store.begin_immediate() as conn:
            store.upsert_unknown_order(
                UnknownOrderRecord(
                    client_order_id="opaca-unknown-leg",
                    proposal_id="unknown-prop",
                    symbol="SGOV",
                    side="SELL",
                    quantity=None,
                    filled_quantity=None,
                    state=OrderState.UNKNOWN.value,
                    last_lookup_at=None,
                    created_at=DEFAULT_NOW,
                ),
                conn=conn,
            )
        gateway = paper_gateway()
        result = reconcile(store, gateway, now=DEFAULT_NOW)
        assert result.status is ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW
        proposal = make_proposal(
            "after-unknown",
            [make_order("after-unknown", 0, "SGOV", Side.BUY, "1", DEFAULT_PRICES["SGOV"])],
        )
        outcome = evaluate_and_reserve(
            store,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
        )
        assert outcome.blocked is True
        assert outcome.reserved is False
        assert outcome.authority_result is not AuthorityResult.AUTO
        store.close()


class TestInvalidBrokerState:
    def test_duplicate_position_symbol_is_invalid(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        result = reconcile(
            store,
            paper_gateway(positions=(position_payload(qty="100"), position_payload(qty="100"))),
            now=DEFAULT_NOW,
        )
        assert result.status is ReconciliationStatus.INVALID_BROKER_STATE
        assert result.snapshot is None
        store.close()

    def test_duplicate_client_order_id_is_invalid(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        result = reconcile(
            store,
            paper_gateway(
                positions=(position_payload(qty="100"),),
                open_orders=(
                    order_payload("dup", status="new"),
                    order_payload("dup", status="new"),
                ),
            ),
            now=DEFAULT_NOW,
        )
        assert result.status is ReconciliationStatus.INVALID_BROKER_STATE
        assert result.snapshot is None
        store.close()

    def test_filled_gt_quantity_is_invalid(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        result = reconcile(
            store,
            paper_gateway(
                positions=(position_payload(qty="100"),),
                open_orders=(
                    order_payload("bad", status="partially_filled", qty="10", filled_qty="50"),
                ),
            ),
            now=DEFAULT_NOW,
        )
        assert result.status is ReconciliationStatus.INVALID_BROKER_STATE
        store.close()

    def test_unknown_order_filled_gt_quantity_is_invalid(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        reconcile(store, paper_gateway(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
        with store.begin_immediate() as conn:
            store.upsert_unknown_order(
                UnknownOrderRecord(
                    client_order_id="bad",
                    proposal_id="p",
                    symbol="SGOV",
                    side="SELL",
                    quantity=Decimal("10"),
                    filled_quantity=Decimal("50"),
                    state=OrderState.PARTIALLY_FILLED.value,
                    last_lookup_at=None,
                    created_at=DEFAULT_NOW,
                ),
                conn=conn,
            )
        result = reconcile(
            store, paper_gateway(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW
        )
        assert result.status is ReconciliationStatus.INVALID_BROKER_STATE
        assert result.snapshot is None or (
            result.snapshot.reconciliation_status is not ReconciliationStatus.RECONCILED
        )
        store.close()


class TestQuantityAvailable:
    def test_unexplained_hold_aside_is_drift(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        reconcile(store, paper_gateway(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
        result = reconcile(
            store,
            paper_gateway(positions=(position_payload(qty="100", qty_available="10"),)),
            now=DEFAULT_NOW,
        )
        assert result.status is ReconciliationStatus.DRIFT_DETECTED
        store.close()

    def test_local_reservation_explains_hold_aside(self, tmp_path: Path) -> None:
        from opaca.orchestration.reserve import evaluate_and_reserve

        store = temp_store(tmp_path)
        recon = reconcile(
            store, paper_gateway(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW
        )
        assert recon.snapshot is not None
        proposal = make_proposal(
            "res",
            [make_order("res", 0, "SGOV", Side.SELL, "10", DEFAULT_PRICES["SGOV"])],
        )
        assert evaluate_and_reserve(
            store,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=recon.snapshot.version,
        ).is_auto
        result = reconcile(
            store,
            paper_gateway(positions=(position_payload(qty="100", qty_available="90"),)),
            now=DEFAULT_NOW,
        )
        assert result.status is ReconciliationStatus.RECONCILED
        store.close()

    def test_known_broker_sell_explains_hold_aside(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        with store.begin_immediate() as conn:
            store.upsert_unknown_order(
                UnknownOrderRecord(
                    client_order_id="known-sell",
                    proposal_id="p",
                    symbol="SGOV",
                    side="SELL",
                    quantity=Decimal("60"),
                    filled_quantity=Decimal("0"),
                    state=OrderState.NEW.value,
                    last_lookup_at=None,
                    created_at=DEFAULT_NOW,
                ),
                conn=conn,
            )
        result = reconcile(
            store,
            paper_gateway(
                positions=(position_payload(qty="100", qty_available="40"),),
                open_orders=(order_payload("known-sell", status="new", qty="60"),),
            ),
            now=DEFAULT_NOW,
        )
        assert result.status is ReconciliationStatus.RECONCILED
        store.close()

    def test_quantity_available_gt_quantity_is_invalid(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        result = reconcile(
            store,
            paper_gateway(positions=(position_payload(qty="100", qty_available="150"),)),
            now=DEFAULT_NOW,
        )
        assert result.status is ReconciliationStatus.INVALID_BROKER_STATE
        store.close()

    def test_fractional_unexplained_hold_aside_is_drift(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        reconcile(
            store,
            paper_gateway(positions=(position_payload(qty="100.5"),)),
            now=DEFAULT_NOW,
        )
        result = reconcile(
            store,
            paper_gateway(positions=(position_payload(qty="100.5", qty_available="100.4"),)),
            now=DEFAULT_NOW,
        )
        assert result.status is ReconciliationStatus.DRIFT_DETECTED
        store.close()
