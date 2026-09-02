"""RED-phase contracts for deterministic post-fill Wheel reconciliation."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from opaca.wheel.models import OptionPosition, OptionPositionSide, OptionRight
from opaca.wheel.reconciliation import (
    WheelAssignmentEvidence,
    WheelBrokerOrder,
    WheelExpiryEvidence,
    WheelReconciliationResult,
    WheelReconciliationStatus,
    WheelState,
    assignment_cash_tolerance,
    reconcile_wheel,
)
from opaca.wheel.store import WheelStore
from tests.wheel.test_atomic_reservation import NOW, authorize, new_store

NEXT_SESSION = date(2026, 9, 4)
EXPIRY = date(2026, 9, 3)
EXPIRY_BUFFER = datetime(2026, 9, 4, 13, 35, tzinfo=UTC)


def broker_order(
    client_order_id: str,
    occ_symbol: str,
    *,
    status: str = "FILLED",
    contracts: int = 1,
    filled_contracts: int = 1,
) -> WheelBrokerOrder:
    return WheelBrokerOrder(
        client_order_id=client_order_id,
        occ_symbol=occ_symbol,
        side="SELL",
        right=OptionRight.PUT,
        status=status,
        contracts=contracts,
        filled_contracts=filled_contracts,
    )


def short_put(occ_symbol: str, *, contracts: int = 1) -> OptionPosition:
    return OptionPosition(
        occ_symbol=occ_symbol,
        underlying="SPY",
        right=OptionRight.PUT,
        contracts=contracts,
        side=OptionPositionSide.SHORT,
    )


def reconcile_open(
    store: WheelStore,
    *,
    order_id: str,
    broker: WheelBrokerOrder | None,
    position: OptionPosition | None,
    account_id: str = "competition-paper",
) -> WheelReconciliationResult:
    expected_symbol = order_id_symbol(store, order_id)
    return reconcile_wheel(
        store,
        account_id=account_id,
        client_order_id=order_id,
        expected_occ_symbol=expected_symbol,
        expected_contracts=1,
        expected_assignment_capital=Decimal("10000"),
        expected_multiplier=Decimal("100"),
        broker_order=broker,
        option_position=position,
        now=NOW,
    )


def test_local_order_without_broker_readback_is_unknown(tmp_path: Path) -> None:
    with new_store(tmp_path / "missing-broker.sqlite3") as store:
        order = authorize(store, "missing-broker")

        result = reconcile_open(store, order_id=order.client_order_id, broker=None, position=None)

        assert result.status is WheelReconciliationStatus.UNKNOWN
        assert result.wheel_state is WheelState.UNKNOWN


def test_broker_order_without_local_authorized_record_is_unknown(tmp_path: Path) -> None:
    with new_store(tmp_path / "missing-local.sqlite3") as store:
        result = reconcile_wheel(
            store,
            account_id="competition-paper",
            client_order_id="broker-only",
            expected_occ_symbol="SPY260904P00100000-broker-only",
            expected_contracts=1,
            expected_assignment_capital=Decimal("10000"),
            expected_multiplier=Decimal("100"),
            broker_order=broker_order("broker-only", "SPY260904P00100000-broker-only"),
            option_position=short_put("SPY260904P00100000-broker-only"),
            now=NOW,
        )

        assert result.status is WheelReconciliationStatus.UNKNOWN
        assert result.wheel_state is WheelState.UNKNOWN


def test_filled_order_without_exact_short_put_position_is_unknown(tmp_path: Path) -> None:
    with new_store(tmp_path / "missing-position.sqlite3") as store:
        order = authorize(store, "missing-position")
        symbol = order_id_symbol(store, order.client_order_id)
        result = reconcile_open(
            store,
            order_id=order.client_order_id,
            broker=broker_order(order.client_order_id, symbol),
            position=None,
        )

        assert result.status is WheelReconciliationStatus.UNKNOWN


def order_id_symbol(store: WheelStore, client_order_id: str) -> str:
    row = store._conn.execute(
        "SELECT occ_symbol FROM wheel_orders WHERE client_order_id = ?",
        (client_order_id,),
    ).fetchone()
    assert row is not None
    return str(row["occ_symbol"])


def test_short_put_without_active_reservation_is_unknown(tmp_path: Path) -> None:
    with new_store(tmp_path / "missing-reservation.sqlite3") as store:
        order = authorize(store, "missing-reservation")
        store.release_assignment_if_proven_no_exposure(
            order.reservation_id,
            proven=True,
            now=NOW,
        )
        symbol = order_id_symbol(store, order.client_order_id)
        result = reconcile_open(
            store,
            order_id=order.client_order_id,
            broker=broker_order(order.client_order_id, symbol),
            position=short_put(symbol),
        )

        assert result.status is WheelReconciliationStatus.UNKNOWN


def test_reservation_amount_mismatch_is_unknown(tmp_path: Path) -> None:
    with new_store(tmp_path / "reservation-mismatch.sqlite3") as store:
        order = authorize(store, "reservation-mismatch")
        with store.begin_immediate() as connection:
            connection.execute(
                "UPDATE wheel_reservations SET amount = ? WHERE reservation_id = ?",
                ("9999", order.reservation_id),
            )
        symbol = order_id_symbol(store, order.client_order_id)
        result = reconcile_open(
            store,
            order_id=order.client_order_id,
            broker=broker_order(order.client_order_id, symbol),
            position=short_put(symbol),
        )

        assert result.status is WheelReconciliationStatus.UNKNOWN


def test_account_fingerprint_mismatch_is_unknown(tmp_path: Path) -> None:
    with new_store(tmp_path / "account-mismatch.sqlite3") as store:
        order = authorize(store, "account-mismatch")
        symbol = order_id_symbol(store, order.client_order_id)
        result = reconcile_open(
            store,
            order_id=order.client_order_id,
            broker=broker_order(order.client_order_id, symbol),
            position=short_put(symbol),
            account_id="different-paper-account",
        )

        assert result.status is WheelReconciliationStatus.UNKNOWN


@pytest.mark.parametrize("mismatch", ["OCC", "QTY"])
def test_contradictory_broker_identity_or_quantity_is_unknown(
    tmp_path: Path,
    mismatch: str,
) -> None:
    with new_store(tmp_path / f"{mismatch.lower()}-mismatch.sqlite3") as store:
        order = authorize(store, f"{mismatch.lower()}-mismatch")
        symbol = order_id_symbol(store, order.client_order_id)
        broker_symbol = symbol if mismatch == "QTY" else symbol + "-other"
        result = reconcile_open(
            store,
            order_id=order.client_order_id,
            broker=broker_order(
                order.client_order_id,
                broker_symbol,
                contracts=2 if mismatch == "QTY" else 1,
                filled_contracts=2 if mismatch == "QTY" else 1,
            ),
            position=short_put(symbol, contracts=2 if mismatch == "QTY" else 1),
        )

        assert result.status is WheelReconciliationStatus.UNKNOWN


def test_all_short_put_facts_agree_and_reconcile(tmp_path: Path) -> None:
    with new_store(tmp_path / "short-put-open.sqlite3") as store:
        order = authorize(store, "short-put-open")
        symbol = order_id_symbol(store, order.client_order_id)
        result = reconcile_open(
            store,
            order_id=order.client_order_id,
            broker=broker_order(order.client_order_id, symbol),
            position=short_put(symbol),
        )

        assert result.status is WheelReconciliationStatus.RECONCILED
        assert result.wheel_state is WheelState.SHORT_PUT_OPEN
        assert store.wheel_state("SPY") is WheelState.SHORT_PUT_OPEN


def seed_assignment_order(store: WheelStore, order_id: str) -> str:
    symbol = "SPY260904P00250000-" + order_id
    reservation_id = "reservation-" + order_id
    store.reserve_assignment(
        reservation_id=reservation_id,
        underlying="SPY",
        amount=Decimal("25000"),
        now=NOW,
    )
    with store.begin_immediate() as connection:
        connection.execute(
            "INSERT INTO wheel_orders "
            "(client_order_id, occ_symbol, status, reservation_id, assignment_capital, "
            "snapshot_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                order_id,
                symbol,
                "AUTHORIZED",
                reservation_id,
                "25000",
                "snapshot-1",
                NOW.isoformat(),
            ),
        )
    return symbol


def assignment_evidence(
    *,
    cash_delta: str = "-25000",
    after_shares: str = "100",
    attributable: bool = True,
    contradictory: bool = False,
) -> WheelAssignmentEvidence:
    return WheelAssignmentEvidence(
        before_shares=Decimal("0"),
        after_shares=Decimal(after_shares),
        cash_delta=Decimal(cash_delta),
        after_market_value=Decimal("25000"),
        shares_attributable=attributable,
        contradictory_activity=contradictory,
    )


def reconcile_assignment(
    store: WheelStore,
    order_id: str,
    *,
    evidence: WheelAssignmentEvidence,
) -> WheelReconciliationResult:
    symbol = "SPY260904P00250000-" + order_id
    return reconcile_wheel(
        store,
        account_id="competition-paper",
        client_order_id=order_id,
        expected_occ_symbol=symbol,
        expected_contracts=1,
        expected_assignment_capital=Decimal("25000"),
        expected_multiplier=Decimal("100"),
        broker_order=broker_order(order_id, symbol, status="FILLED"),
        option_position=None,
        assignment=evidence,
        now=NOW,
    )


def test_assignment_cash_tolerance_is_twelve_fifty_at_twenty_five_thousand() -> None:
    assert assignment_cash_tolerance(Decimal("25000")) == Decimal("12.50")


def test_assignment_within_cash_tolerance_creates_lot_before_release(tmp_path: Path) -> None:
    with new_store(tmp_path / "assignment-valid.sqlite3") as store:
        order_id = "assignment-valid"
        seed_assignment_order(store, order_id)
        result = reconcile_assignment(
            store,
            order_id,
            evidence=assignment_evidence(cash_delta="-25010"),
        )

        assert result.status is WheelReconciliationStatus.RECONCILED
        assert result.wheel_state is WheelState.SHARES_HELD
        assert store.active_assignment_reservations() == []
        assert store.share_lots()[0].shares == 100


@pytest.mark.parametrize(
    "evidence",
    [
        assignment_evidence(cash_delta="-25020"),
        assignment_evidence(attributable=False),
        assignment_evidence(after_shares="50"),
    ],
)
def test_assignment_contradiction_or_unexplained_share_delta_is_unknown(
    tmp_path: Path,
    evidence: WheelAssignmentEvidence,
) -> None:
    with new_store(tmp_path / "assignment-unknown.sqlite3") as store:
        order_id = "assignment-unknown"
        seed_assignment_order(store, order_id)
        result = reconcile_assignment(store, order_id, evidence=evidence)

        assert result.status is WheelReconciliationStatus.UNKNOWN
        assert store.active_assignment_reservations()
        assert store.share_lots() == []


def expiry_evidence(*, assignment_evidence_present: bool = False) -> WheelExpiryEvidence:
    return WheelExpiryEvidence(
        before_shares=Decimal("0"),
        after_shares=Decimal("0"),
        cash_delta=Decimal("0"),
        assignment_evidence_present=assignment_evidence_present,
    )


def reconcile_expiry(
    store: WheelStore,
    order_id: str,
    *,
    now: datetime,
    evidence: WheelExpiryEvidence,
) -> WheelReconciliationResult:
    symbol = "SPY260904P00250000-" + order_id
    return reconcile_wheel(
        store,
        account_id="competition-paper",
        client_order_id=order_id,
        expected_occ_symbol=symbol,
        expected_contracts=1,
        expected_assignment_capital=Decimal("25000"),
        expected_multiplier=Decimal("100"),
        broker_order=broker_order(
            order_id,
            symbol,
            status="EXPIRED",
            contracts=1,
            filled_contracts=0,
        ),
        option_position=None,
        expiration=EXPIRY,
        next_regular_session=lambda _expiration: NEXT_SESSION,
        expiry=evidence,
        now=now,
    )


def test_expiry_before_next_session_buffer_is_unknown(tmp_path: Path) -> None:
    with new_store(tmp_path / "expiry-before-buffer.sqlite3") as store:
        order_id = "expiry-before-buffer"
        seed_assignment_order(store, order_id)
        result = reconcile_expiry(
            store,
            order_id,
            now=EXPIRY_BUFFER - timedelta(seconds=1),
            evidence=expiry_evidence(),
        )

        assert result.status is WheelReconciliationStatus.UNKNOWN
        assert store.active_assignment_reservations()


def test_expiry_at_next_session_buffer_releases_reservation_and_returns_cash(
    tmp_path: Path,
) -> None:
    with new_store(tmp_path / "expiry-after-buffer.sqlite3") as store:
        order_id = "expiry-after-buffer"
        seed_assignment_order(store, order_id)
        result = reconcile_expiry(
            store,
            order_id,
            now=EXPIRY_BUFFER,
            evidence=expiry_evidence(),
        )

        assert result.status is WheelReconciliationStatus.RECONCILED
        assert result.wheel_state is WheelState.CASH
        assert store.active_assignment_reservations() == []


def test_expiry_with_assignment_evidence_remains_unknown(tmp_path: Path) -> None:
    with new_store(tmp_path / "expiry-assignment-conflict.sqlite3") as store:
        order_id = "expiry-assignment-conflict"
        seed_assignment_order(store, order_id)
        result = reconcile_expiry(
            store,
            order_id,
            now=EXPIRY_BUFFER,
            evidence=expiry_evidence(assignment_evidence_present=True),
        )

        assert result.status is WheelReconciliationStatus.UNKNOWN
        assert store.active_assignment_reservations()
