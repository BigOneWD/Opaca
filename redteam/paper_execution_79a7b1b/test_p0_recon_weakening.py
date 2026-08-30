"""P0: the fill-explains-position-delta exception must not blind reconciliation.

Phase 2 flagged ANY position change versus the prior snapshot as DRIFT.
Phase 3 relaxes that: a delta exactly equal to locally recorded, not-yet-stamped
fills is accepted as RECONCILED. These probes bound that relaxation.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from opaca.persistence.types import ReconciliationStatus
from opaca.reconciliation.service import compare_state, reconcile

from p3_support import DEFAULT_NOW, DEFAULT_PRICES, active_sell_qty, reserve, sell, world


def _execute(w, proposal, gw=None, **kw):
    from opaca.execution.service import execute_reserved_proposal

    return execute_reserved_proposal(
        w.store, w.read(), gw if gw is not None else w.mutate(**kw),
        proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
    )


def test_r01_our_own_fill_does_not_raise_drift(tmp_path):
    """The intended relaxation: a delta matching our recorded fill reconciles."""
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    result = _execute(w, proposal)
    assert result.state.value == "FILLED"
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    assert recon.status is ReconciliationStatus.RECONCILED, recon.reasons
    w.close()


def test_r02_an_external_change_on_top_of_our_fill_is_drift(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    _execute(w, proposal)
    # someone else moves 5 more shares before we reconcile
    w.positions[0]["qty"] = format(Decimal(str(w.positions[0]["qty"])) - Decimal("5"), "f")
    w.positions[0]["qty_available"] = w.positions[0]["qty"]
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    assert recon.status is ReconciliationStatus.DRIFT_DETECTED, recon.reasons
    w.close()


def test_r03_an_external_change_in_the_opposite_direction_is_drift(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    _execute(w, proposal)
    w.positions[0]["qty"] = format(Decimal(str(w.positions[0]["qty"])) + Decimal("3"), "f")
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    assert recon.status is ReconciliationStatus.DRIFT_DETECTED, recon.reasons
    w.close()


def test_r04_the_exception_is_consumed_once(tmp_path):
    """After a RECONCILED stamp, the same fill may not explain a second delta."""
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    _execute(w, proposal)
    assert reconcile(w.store, w.read(), now=DEFAULT_NOW).status is (
        ReconciliationStatus.RECONCILED
    )
    w.positions[0]["qty"] = format(Decimal(str(w.positions[0]["qty"])) - Decimal("10"), "f")
    w.positions[0]["qty_available"] = w.positions[0]["qty"]
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    assert recon.status is ReconciliationStatus.DRIFT_DETECTED, recon.reasons
    w.close()


def test_r05_a_purely_external_change_with_no_local_fill_is_drift(tmp_path):
    w = world(tmp_path)
    w.positions[0]["qty"] = "90"
    w.positions[0]["qty_available"] = "90"
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    assert recon.status is ReconciliationStatus.DRIFT_DETECTED, recon.reasons
    w.close()


def test_r06_a_disappearing_symbol_is_drift(tmp_path):
    w = world(tmp_path)
    w.positions.clear()
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    assert recon.status is ReconciliationStatus.DRIFT_DETECTED, recon.reasons
    w.close()


# ------------------------------------------------------------------- FINDING


def test_a_zero_net_delta_is_compared_against_local_fills(tmp_path):
    """CLOSED at cd3dc86 (was P1-1). compare_state no longer short-circuits on a raw
    delta of zero: it compares `delta == expected` for every symbol, including symbols
    that appear only in the explained set."""
    from opaca.domain.models import BrokerCashState, Position, Side
    from opaca.persistence.types import ExecutionOrderRecord, ExecutionState, PersistedSnapshot

    position = Position(
        symbol="SGOV", quantity=Decimal("100"), quantity_available=Decimal("100"),
        market_value=Decimal("10069"),
    )
    broker = BrokerCashState(
        cash=Decimal("100000"), buying_power=Decimal("400000"),
        non_marginable_buying_power=Decimal("100000"), multiplier=Decimal("4"),
        as_of=DEFAULT_NOW,
    )
    previous = PersistedSnapshot(
        snapshot_id=1, version=1, broker=broker, positions=(position,), assets=(), orders=(),
        reconciliation_status=ReconciliationStatus.RECONCILED, captured_at=DEFAULT_NOW,
        diagnostics="{}",
    )
    order = ExecutionOrderRecord(
        client_order_id="opaca-" + "0" * 32, proposal_id="s1", leg_index=0, symbol="SGOV",
        side=Side.SELL, quantity=Decimal("10"), filled_quantity=Decimal("10"),
        remaining_quantity=Decimal("0"), state=ExecutionState.FILLED, broker_order_id="b1",
        last_broker_status="filled", filled_avg_price=Decimal("100.69"),
        reference_price=Decimal("100.69"), reconciled_filled_quantity=Decimal("0"),
        settled_proceeds=Decimal("0"), created_at=DEFAULT_NOW, updated_at=DEFAULT_NOW,
    )
    status, reasons = compare_state(
        broker=broker, positions=(position,), orders=(), previous=previous,
        reservations=(), unknown_orders=(), settlement_events=(),
        as_of=DEFAULT_NOW.date(), execution_orders=(order,),
    )
    assert status is ReconciliationStatus.DRIFT_DETECTED
    assert any("not reflected in broker position" in r for r in reasons), reasons

    # and a genuine no-change with nothing explained is still RECONCILED
    clean, _ = compare_state(
        broker=broker, positions=(position,), orders=(), previous=previous,
        reservations=(), unknown_orders=(), settlement_events=(),
        as_of=DEFAULT_NOW.date(), execution_orders=(),
    )
    assert clean is ReconciliationStatus.RECONCILED


def test_an_offsetting_external_change_is_drift(tmp_path):
    """CLOSED at cd3dc86 (was P1-1, end to end). Our 10-share sell fills and an
    external +10 restores the position: the net zero delta is now examined."""
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    _execute(w, proposal)
    w.positions[0]["qty"] = "100"
    w.positions[0]["qty_available"] = "100"
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    status, reasons = recon.status, recon.reasons
    w.close()
    assert status is ReconciliationStatus.DRIFT_DETECTED, reasons
