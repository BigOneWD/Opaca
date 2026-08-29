"""P1-B: reconciliation state distinctions are real; UNKNOWN is never executable."""

from __future__ import annotations

import sqlite3

import pytest
from opaca.broker.errors import InvalidBrokerStateError
from opaca.broker.gateway import FakeAlpacaGateway
from opaca.domain.models import OrderState, Side
from opaca.persistence.types import ReconciliationStatus, UnknownOrderRecord
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


def test_FINDING_unexplained_broker_hold_aside_without_local_reservation(tmp_path):
    """quantity_available << quantity with NO local reservation is silently RECONCILED:
    the consistency loop is keyed on local reservations, so unexplained broker-side
    encumbrance produces no drift signal."""
    store = temp_store(tmp_path)
    reconcile(store, gw(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
    r = reconcile(
        store,
        gw(positions=(position_payload(qty="100", qty_available="10"),)),
        now=DEFAULT_NOW,
    )
    assert r.status is ReconciliationStatus.RECONCILED, "probe assumption"
    pytest.fail(
        "FINDING P1-B-4: broker reports 90 of 100 shares held aside with no local "
        "reservation and no broker order; reconciliation returns RECONCILED "
        "(detection gap; CHECK-16 still bounds sells by quantity_available)"
    )


def test_partial_fill_at_broker_never_reconciles_into_executable_state(tmp_path):
    store, v = reconciled_store(tmp_path, qty="100")
    r = reconcile(
        store,
        gw(positions=(position_payload(qty="100"),),
           open_orders=(order_payload("pf", status="partially_filled", qty="50",
                                      filled_qty="20"),)),
        now=DEFAULT_NOW,
    )
    assert r.status is not ReconciliationStatus.RECONCILED
    store.close()


def test_canceled_with_remainder_is_not_unresolved(tmp_path):
    store = temp_store(tmp_path)
    r = reconcile(
        store,
        gw(positions=(position_payload(qty="100"),),
           open_orders=(order_payload("c", status="canceled", qty="50", filled_qty="20"),)),
        now=DEFAULT_NOW,
    )
    assert r.status is ReconciliationStatus.RECONCILED
    store.close()


# ---------------------------------------------------------------- FINDINGS


def test_FINDING_duplicate_broker_records_crash_reconcile(tmp_path):
    """Duplicate rows raise a raw sqlite3.IntegrityError instead of INVALID_BROKER_STATE."""
    store = temp_store(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        reconcile(
            store,
            gw(positions=(position_payload(qty="100"), position_payload(qty="100"))),
            now=DEFAULT_NOW,
        )
    assert store.latest_snapshot() is None
    store.close()
    pytest.fail(
        "FINDING P1-B-1: duplicate broker position rows escape reconcile() as a raw "
        "sqlite3.IntegrityError; there is no INVALID_BROKER_STATE classification for "
        "duplicate broker records"
    )


def test_FINDING_duplicate_client_order_id_crashes_reconcile(tmp_path):
    store = temp_store(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        reconcile(
            store,
            gw(positions=(position_payload(qty="100"),),
               open_orders=(order_payload("dup", status="new"),
                            order_payload("dup", status="new"))),
            now=DEFAULT_NOW,
        )
    store.close()
    pytest.fail(
        "FINDING P1-B-2: duplicate broker client_order_id escapes reconcile() as a raw "
        "sqlite3.IntegrityError rather than a reconciliation status"
    )


def test_FINDING_filled_gt_quantity_reconciles_then_explodes_later(tmp_path):
    """A broker order with filled_qty > qty is accepted by reconciliation and only
    raises when the policy context is built."""
    from opaca.orchestration.reserve import evaluate_and_reserve

    store, _ = reconciled_store(tmp_path, qty="100")
    with store.begin_immediate() as conn:
        store.upsert_unknown_order(
            UnknownOrderRecord(
                client_order_id="bad", proposal_id="p", symbol="SGOV", side="SELL",
                quantity=__import__("decimal").Decimal("10"),
                filled_quantity=__import__("decimal").Decimal("50"),
                state=OrderState.PARTIALLY_FILLED.value,
                last_lookup_at=None, created_at=DEFAULT_NOW,
            ),
            conn=conn,
        )
    r = reconcile(store, gw(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
    persisted_status = r.status
    assert persisted_status is ReconciliationStatus.RECONCILED, persisted_status
    p = make_proposal("later", [make_order("later", 0, "SGOV", Side.SELL, "1", SGOV)])
    raised = None
    try:
        evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                             expected_snapshot_version=r.snapshot.version)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    store.close()
    assert raised is not None, "probe assumption: context build must fail"
    pytest.fail(
        f"FINDING P1-B-3: filled_quantity > quantity persisted with status "
        f"{persisted_status.value}; the contradiction surfaces later as "
        f"{type(raised).__name__} out of evaluate_and_reserve, not as INVALID_BROKER_STATE"
    )
