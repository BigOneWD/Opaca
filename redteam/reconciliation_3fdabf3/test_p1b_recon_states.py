"""P1-B: reconciliation state distinctions are real; UNKNOWN is never executable."""

from __future__ import annotations

from decimal import Decimal

import pytest
from opaca.broker.gateway import FakeAlpacaGateway
from opaca.domain.models import OrderState, Side
from opaca.persistence.types import (
    AuditEventType,
    ReconciliationStatus,
    UnknownOrderRecord,
)
from opaca.reconciliation.service import reconcile
from tests.state_helpers import PHASE1_ASSETS, account_payload, order_payload

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

SGOV = DEFAULT_PRICES["SGOV"]


def gw(**kw):
    base = dict(
        account=account_payload(),
        positions=(),
        assets=PHASE1_ASSETS,
        open_orders=(),
        orders_by_client_id={},
        clock={"timestamp": DEFAULT_NOW.isoformat(), "is_open": True},
    )
    base.update(kw)
    return FakeAlpacaGateway(**base)


def test_reconciled_baseline(tmp_path):
    store = temp_store(tmp_path)
    r = reconcile(store, gw(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
    assert r.status is ReconciliationStatus.RECONCILED
    store.close()


def test_broker_unavailable_is_its_own_state(tmp_path):
    store = temp_store(tmp_path)
    r = reconcile(store, paper_gateway(unavailable=True), now=DEFAULT_NOW)
    assert r.status is ReconciliationStatus.BROKER_UNAVAILABLE
    assert r.snapshot is None
    store.close()


def test_invalid_broker_state_is_its_own_state(tmp_path):
    store = temp_store(tmp_path)
    bad = dict(account_payload())
    bad["cash"] = "NaN"
    r = reconcile(store, gw(account=bad), now=DEFAULT_NOW)
    assert r.status is ReconciliationStatus.INVALID_BROKER_STATE
    assert r.snapshot is None
    store.close()


def test_unmapped_status_is_unknown_requires_review(tmp_path):
    store = temp_store(tmp_path)
    r = reconcile(
        store,
        gw(positions=(position_payload(qty="100"),),
           open_orders=(order_payload("x", status="frobnicated"),)),
        now=DEFAULT_NOW,
    )
    assert r.status is ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW
    store.close()


def test_broker_unresolved_order_unknown_locally_is_drift(tmp_path):
    store = temp_store(tmp_path)
    r = reconcile(
        store,
        gw(positions=(position_payload(qty="100"),),
           open_orders=(order_payload("stranger", status="new"),)),
        now=DEFAULT_NOW,
    )
    assert r.status is ReconciliationStatus.DRIFT_DETECTED
    store.close()


def test_local_submitted_absent_at_broker_is_unknown(tmp_path):
    store = temp_store(tmp_path)
    with store.begin_immediate() as conn:
        store.upsert_unknown_order(
            UnknownOrderRecord(
                client_order_id="gone",
                proposal_id="p",
                symbol="SGOV",
                side="SELL",
                quantity=None,
                filled_quantity=None,
                state=OrderState.SUBMITTED.value,
                last_lookup_at=None,
                created_at=DEFAULT_NOW,
            ),
            conn=conn,
        )
    r = reconcile(store, gw(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
    assert r.status is ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW
    store.close()


def test_unknown_lookup_not_found_becomes_requires_review(tmp_path):
    store = temp_store(tmp_path)
    with store.begin_immediate() as conn:
        store.upsert_unknown_order(
            UnknownOrderRecord(
                client_order_id="ghost", proposal_id="p", symbol="SGOV", side="SELL",
                quantity=None, filled_quantity=None, state=OrderState.UNKNOWN.value,
                last_lookup_at=None, created_at=DEFAULT_NOW,
            ),
            conn=conn,
        )
    r = reconcile(store, gw(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
    assert r.status is ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW
    store.close()


def test_unknown_lookup_unavailable_becomes_requires_review(tmp_path):
    store = temp_store(tmp_path)
    with store.begin_immediate() as conn:
        store.upsert_unknown_order(
            UnknownOrderRecord(
                client_order_id="ghost", proposal_id="p", symbol="SGOV", side="SELL",
                quantity=None, filled_quantity=None, state=OrderState.UNKNOWN.value,
                last_lookup_at=None, created_at=DEFAULT_NOW,
            ),
            conn=conn,
        )
    g = gw(positions=(position_payload(qty="100"),))
    g.lookup_unavailable = True
    r = reconcile(store, g, now=DEFAULT_NOW)
    assert r.status is ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW
    store.close()


def test_cash_inconsistent_with_unsettled_proceeds_is_drift(tmp_path):
    from datetime import timedelta
    from decimal import Decimal

    from opaca.domain.models import SettlementEvent

    store = temp_store(tmp_path)
    store.insert_settlement_event(
        SettlementEvent(
            event_id="e1", symbol="SGOV", trade_date=DEFAULT_NOW.date(),
            settlement_date=DEFAULT_NOW.date() + timedelta(days=1), amount=Decimal("200000"),
        ),
        now=DEFAULT_NOW,
    )
    r = reconcile(store, gw(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
    assert r.status is ReconciliationStatus.DRIFT_DETECTED
    store.close()


def test_local_reservation_without_broker_position_is_drift(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    from opaca.orchestration.reserve import evaluate_and_reserve

    p = make_proposal("s", [make_order("s", 0, "SGOV", Side.SELL, "10", SGOV)])
    out = evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                               expected_snapshot_version=v)
    assert out.is_auto
    r = reconcile(store, gw(positions=()), now=DEFAULT_NOW)
    assert r.status is not ReconciliationStatus.RECONCILED
    store.close()


def test_quantity_available_inconsistent_with_reservations_is_drift(tmp_path):
    """With a local reservation present, an over-large broker hold-aside is drift."""
    from opaca.orchestration.reserve import evaluate_and_reserve

    store, v = reconciled_store(tmp_path, qty="100")
    p = make_proposal("res", [make_order("res", 0, "SGOV", Side.SELL, "10", SGOV)])
    assert evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                                expected_snapshot_version=v).is_auto
    r = reconcile(
        store,
        gw(positions=(position_payload(qty="100", qty_available="10"),)),
        now=DEFAULT_NOW,
    )
    assert r.status is ReconciliationStatus.DRIFT_DETECTED
    store.close()


def test_unexplained_broker_hold_aside_is_drift(tmp_path):
    """CLOSED at d85a2e6 (was P1-B-4). The hold-aside consistency check no longer
    keys on local reservations: an unexplained reduction is drift on its own."""
    store = temp_store(tmp_path)
    reconcile(store, gw(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
    r = reconcile(
        store,
        gw(positions=(position_payload(qty="100", qty_available="10"),)),
        now=DEFAULT_NOW,
    )
    assert r.status is ReconciliationStatus.DRIFT_DETECTED
    assert any("unexplained hold-aside" in reason for reason in r.reasons), r.reasons
    store.close()


def test_duplicate_broker_position_rows_are_invalid_broker_state(tmp_path):
    """CLOSED at d85a2e6 (was P1-B-1). Duplicates were a raw sqlite3.IntegrityError."""
    store = temp_store(tmp_path)
    r = reconcile(
        store,
        gw(positions=(position_payload(qty="100"), position_payload(qty="100"))),
        now=DEFAULT_NOW,
    )
    assert r.status is ReconciliationStatus.INVALID_BROKER_STATE
    assert r.snapshot is None
    assert store.latest_snapshot() is None
    assert AuditEventType.INVALID_BROKER_STATE in [e.event_type for e in store.list_audit()]
    store.close()


def test_duplicate_client_order_id_is_invalid_broker_state(tmp_path):
    """CLOSED at d85a2e6 (was P1-B-2)."""
    store = temp_store(tmp_path)
    r = reconcile(
        store,
        gw(positions=(position_payload(qty="100"),),
           open_orders=(order_payload("dup", status="new"), order_payload("dup", status="new"))),
        now=DEFAULT_NOW,
    )
    assert r.status is ReconciliationStatus.INVALID_BROKER_STATE
    assert r.snapshot is None
    store.close()


def test_filled_greater_than_quantity_is_invalid_at_the_boundary(tmp_path):
    """CLOSED at d85a2e6 (was P1-B-3). The contradiction was persisted as
    RECONCILED and only surfaced later as a raw ValueError from the orchestrator."""
    store, _ = reconciled_store(tmp_path, qty="100")
    before = store.latest_snapshot()
    with store.begin_immediate() as conn:
        store.upsert_unknown_order(
            UnknownOrderRecord(
                client_order_id="bad", proposal_id="p", symbol="SGOV", side="SELL",
                quantity=Decimal("10"), filled_quantity=Decimal("50"),
                state=OrderState.PARTIALLY_FILLED.value,
                last_lookup_at=None, created_at=DEFAULT_NOW,
            ),
            conn=conn,
        )
    r = reconcile(store, gw(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
    assert r.status is ReconciliationStatus.INVALID_BROKER_STATE
    assert r.snapshot is None
    assert store.latest_snapshot() == before, "the last good snapshot must be untouched"

    (tmp_path / "b").mkdir()
    fresh = temp_store(tmp_path / "b")
    r2 = reconcile(
        fresh,
        gw(positions=(position_payload(qty="100"),),
           open_orders=(order_payload("pf", status="partially_filled", qty="10",
                                      filled_qty="50"),)),
        now=DEFAULT_NOW,
    )
    assert r2.status is ReconciliationStatus.INVALID_BROKER_STATE
    fresh.close()
    store.close()


def test_no_raw_exception_escapes_reconcile_for_any_corrupt_shape(tmp_path):
    """The reconciliation boundary must translate, never propagate."""
    shapes = [
        dict(positions=(position_payload(qty="100"), position_payload(qty="100"))),
        dict(positions=(position_payload(qty="100", qty_available="150"),)),
        dict(positions=({**position_payload(), "qty": "NaN"},)),
        dict(open_orders=(order_payload("dup", status="new"), order_payload("dup", status="new"))),
        dict(open_orders=(order_payload("f", status="filled", qty="1", filled_qty="2"),)),
        dict(open_orders=(order_payload("s", status="new", side="sideways"),)),
        dict(account={**account_payload(), "cash": "NaN"}),
        dict(account={**account_payload(), "cash": "not a number"}),
    ]
    for index, shape in enumerate(shapes):
        directory = tmp_path / f"s{index}"
        directory.mkdir()
        store = temp_store(directory)
        try:
            result = reconcile(store, gw(**shape), now=DEFAULT_NOW)
        except BaseException as exc:  # noqa: BLE001
            pytest.fail(f"{shape} raised {type(exc).__name__}: {exc}")
        assert result.status in {
            ReconciliationStatus.INVALID_BROKER_STATE,
            ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW,
            ReconciliationStatus.DRIFT_DETECTED,
        }
        assert result.status is not ReconciliationStatus.RECONCILED
        store.close()
