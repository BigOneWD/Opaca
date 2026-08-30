"""P0: reservations shrink only against proven broker disposition, never below truth."""

from __future__ import annotations

from decimal import Decimal

import pytest
from opaca.domain.models import Side
from opaca.execution.service import cancel_remaining, execute_reserved_proposal
from opaca.persistence.types import (
    AuditEventType,
    ExecutionState,
    ReconciliationStatus,
    ReservationKind,
    ReservationStatus,
)
from opaca.reconciliation.service import reconcile

from p3_support import (
    DEFAULT_NOW,
    DEFAULT_PRICES,
    SGOV,
    active_cash,
    active_sell_qty,
    buy,
    make_order,
    make_proposal,
    reserve,
    sell,
    world,
)


def _execute(w, proposal, gw=None, **kw):
    return execute_reserved_proposal(
        w.store, w.read(), gw if gw is not None else w.mutate(**kw),
        proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
    )


def test_v01_full_fill_releases_the_sell_reservation(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    assert active_sell_qty(w.store) == Decimal("10")
    result = _execute(w, proposal)
    assert result.state is ExecutionState.FILLED
    assert active_sell_qty(w.store) == Decimal("0")
    assert result.reservation_active is False
    assert AuditEventType.RESERVATION_RELEASED in [
        e.event_type for e in w.store.list_audit(proposal_id="s1")
    ]
    w.close()


def test_v02_partial_fill_resizes_to_the_remainder_exactly(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    result = _execute(w, proposal, partial_fill_qty=Decimal("4"))
    assert result.state is ExecutionState.PARTIALLY_FILLED
    assert result.filled_quantity == Decimal("4")
    assert active_sell_qty(w.store) == Decimal("6"), "exactly the unfilled remainder"
    assert AuditEventType.PARTIAL_FILL in [
        e.event_type for e in w.store.list_audit(proposal_id="s1")
    ]
    w.close()


def test_v03_partial_fill_never_releases_more_than_was_filled(tmp_path):
    for filled, remaining in (("1", "9"), ("9", "1"), ("0.000000001", "9.999999999")):
        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp())
        w = world(tmp)
        proposal = sell("s1", "10")
        reserve(w, proposal)
        _execute(w, proposal, partial_fill_qty=Decimal(filled))
        assert active_sell_qty(w.store) == Decimal(remaining), filled
        w.close()


def test_v04_zero_fill_retains_the_whole_reservation(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    result = _execute(w, proposal, fill_on_submit=False)
    assert result.state is ExecutionState.SUBMITTED
    assert active_sell_qty(w.store) == Decimal("10")
    w.close()


def test_v05_rejection_releases_capacity(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    result = _execute(w, proposal, reject_reason="insufficient shares")
    assert result.state is ExecutionState.REJECTED
    assert active_sell_qty(w.store) == Decimal("0"), "a rejected order consumes nothing"
    w.close()


def test_v06_cancel_after_partial_fill_releases_only_the_dead_remainder(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    coid = proposal.legs[0].client_order_id
    gw = w.mutate(partial_fill_qty=Decimal("4"))
    _execute(w, proposal, gw)
    assert active_sell_qty(w.store) == Decimal("6")
    result = cancel_remaining(w.store, w.read(), gw, coid, now=DEFAULT_NOW)
    assert result.state is ExecutionState.CANCELLED
    assert result.filled_quantity == Decimal("4")
    assert active_sell_qty(w.store) == Decimal("0"), "a cancelled order can consume no more"
    w.close()


def test_v07_cancelled_remainder_does_not_resurrect_capacity(tmp_path):
    """Releasing the remainder must not put the 4 filled shares back."""
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    gw = w.mutate(partial_fill_qty=Decimal("4"))
    _execute(w, proposal, gw)
    cancel_remaining(w.store, w.read(), gw, proposal.legs[0].client_order_id, now=DEFAULT_NOW)
    recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
    assert recon.status is ReconciliationStatus.RECONCILED, recon.reasons
    assert recon.snapshot.positions[0].quantity == Decimal("96")
    w.close()


def test_v08_unknown_blocks_every_release_in_the_proposal(tmp_path):
    """Two legs, one lost at the broker. Nothing may be released."""
    from opaca.persistence.store import SQLiteStore
    from opaca.reconciliation.service import reconcile as _reconcile

    from p3_support import World
    from tests.state_helpers import account_payload, position_payload

    store = SQLiteStore(tmp_path / "opaca.sqlite")
    w = World(
        store=store,
        account=dict(account_payload(cash="100000")),
        positions=[
            dict(position_payload(qty="100")),
            {"symbol": "BIL", "side": "long", "qty": "50", "qty_available": "50",
             "market_value": "4600"},
        ],
    )
    assert _reconcile(store, w.read(), now=DEFAULT_NOW).status is (
        ReconciliationStatus.RECONCILED
    )
    legs = [
        make_order("m1", 0, "SGOV", Side.SELL, "10", SGOV),
        make_order("m1", 1, "BIL", Side.SELL, "1", DEFAULT_PRICES["BIL"]),
    ]
    proposal = make_proposal("m1", legs)
    _, reserved = reserve(w, proposal)
    if not reserved.is_auto:
        pytest.skip(f"multi-leg proposal not AUTO in this scenario: {reserved.authority_result}")
    before_sgov = active_sell_qty(w.store, "SGOV")
    before_bil = active_sell_qty(w.store, "BIL")
    assert before_sgov == Decimal("10") and before_bil == Decimal("1")
    gw = w.mutate(timeout_before_accept=True)
    _execute(w, proposal, gw)
    assert active_sell_qty(w.store, "SGOV") == before_sgov
    assert active_sell_qty(w.store, "BIL") == before_bil
    w.close()


def test_v09_buy_cash_reservation_releases_on_fill(tmp_path):
    w = world(tmp_path, qty="0", cash="100000")
    proposal = buy("b1", "10")
    _, reserved = reserve(w, proposal)
    assert reserved.is_auto
    assert active_cash(w.store) == proposal.total_buy_notional
    result = _execute(w, proposal)
    assert result.state is ExecutionState.FILLED
    assert active_cash(w.store) == Decimal("0")
    w.close()


def test_v10_buy_cash_reservation_resizes_on_partial_fill(tmp_path):
    w = world(tmp_path, qty="0", cash="100000")
    proposal = buy("b1", "10")
    reserve(w, proposal)
    _execute(w, proposal, partial_fill_qty=Decimal("4"))
    expected = (Decimal("6") * SGOV).quantize(Decimal("0.01"))
    assert active_cash(w.store) == expected, active_cash(w.store)
    w.close()


def test_v11_released_reservations_are_marked_not_deleted(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    _execute(w, proposal)
    rows = w.store._conn.execute(
        "SELECT kind, status FROM reservations WHERE proposal_id='s1'"
    ).fetchall()
    assert rows, "reservation rows must be retained for audit"
    assert any(r["status"] == ReservationStatus.RELEASED.value for r in rows)
    w.close()


def test_v12_release_is_idempotent_across_repeated_recovery(tmp_path):
    from opaca.execution.service import recover_proposal

    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    _execute(w, proposal)
    for _ in range(3):
        recover_proposal(w.store, w.read(), "s1", now=DEFAULT_NOW)
    assert active_sell_qty(w.store) == Decimal("0")
    released = [
        e for e in w.store.list_audit(proposal_id="s1")
        if e.event_type is AuditEventType.RESERVATION_RELEASED
    ]
    assert len(released) <= 4, f"release audited {len(released)} times"
    w.close()


# ------------------------------------------------------- the oversell attack


def test_v13_a_released_reservation_cannot_be_reused_to_oversell(tmp_path):
    """Sell 60 of 100 and fill it. A second proposal may sell at most the real 40."""
    w = world(tmp_path, qty="100")
    first = sell("s1", "60")
    _, r1 = reserve(w, first)
    assert r1.is_auto
    assert _execute(w, first).state is ExecutionState.FILLED
    assert active_sell_qty(w.store) == Decimal("0")
    assert Decimal(str(w.positions[0]["qty"])) == Decimal("40")

    too_big = sell("s2", "60")
    _, r2 = reserve(w, too_big)
    assert not r2.is_auto, "40 shares remain; a 60-share sell must not be authorised"

    ok = sell("s3", "40")
    _, r3 = reserve(w, ok)
    assert r3.is_auto
    assert _execute(w, ok).state is ExecutionState.FILLED
    remaining = (
        Decimal("0") if not w.positions else Decimal(str(w.positions[0]["qty"]))
    )
    assert remaining == Decimal("0")
    w.close()


def test_v14_partial_fill_bounds_a_second_proposal(tmp_path):
    w = world(tmp_path, qty="100")
    first = sell("s1", "60")
    reserve(w, first)
    _execute(w, first, partial_fill_qty=Decimal("20"))
    # 20 sold, 40 still reserved and live at the broker, 80 shares remain
    assert active_sell_qty(w.store) == Decimal("40")
    assert Decimal(str(w.positions[0]["qty"])) == Decimal("80")
    second = sell("s2", "41")
    _, r2 = reserve(w, second)
    assert not r2.is_auto, "80 held minus 40 genuinely encumbered leaves 40; 41 must fail"
    w.close()


def test_FINDING_a_live_order_is_counted_twice_after_a_partial_fill(tmp_path):
    """The resized reservation and the broker order it produced are the SAME
    encumbrance, but build_policy_context feeds both into CHECK-16, so the
    remaining quantity is subtracted twice."""
    from opaca.orchestration.context import build_policy_context
    from opaca.policy.engine import sell_reservations

    w = world(tmp_path, qty="100")
    first = sell("s1", "60")
    reserve(w, first)
    _execute(w, first, partial_fill_qty=Decimal("20"))
    assert active_sell_qty(w.store) == Decimal("40"), "one genuine encumbrance of 40"
    assert Decimal(str(w.positions[0]["qty"])) == Decimal("80")

    # a fresh reconcile captures the live broker order into the snapshot, exactly as
    # the reserve and execute paths do
    assert reconcile(w.store, w.read(), now=DEFAULT_NOW).status is (
        ReconciliationStatus.RECONCILED
    )
    context, _ = build_policy_context(w.store, now=DEFAULT_NOW, prices=DEFAULT_PRICES)
    reserved, _undeterminable = sell_reservations(context.unresolved_orders)
    counted = reserved.get("SGOV", Decimal("0"))
    sources = sorted(
        (o.proposal_id, str(o.remaining_quantity)) for o in context.unresolved_orders
        if o.symbol == "SGOV" and o.is_unresolved
    )

    third = sell("s3", "40")
    _, r3 = reserve(w, third)
    w.close()
    assert counted == Decimal("80"), f"probe assumption: counted {counted}"
    assert r3.is_auto is False, "probe assumption"
    pytest.fail(
        "FINDING P1-2: after a partial fill the same live order is counted twice - "
        f"once as the resized SELL_QUANTITY reservation and once as an unresolved "
        f"broker order, giving {counted} against a real encumbrance of 40. "
        f"Sources: {sources}. "
        "CHECK-16's documented bound is min(broker available, quantity - reserved) and "
        "its docstring promises 'never a double subtraction of the reservation'. "
        "Fail-closed - it under-permits - but it freezes the symbol: no further sell of "
        "any size can be authorised while a partially filled order is live."
    )


def test_v15_capacity_is_never_negative(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    _execute(w, proposal)
    for r in w.store._conn.execute(
        "SELECT quantity, amount FROM reservations WHERE proposal_id='s1'"
    ):
        if r["quantity"] is not None:
            assert Decimal(str(r["quantity"])) >= 0
        if r["amount"] is not None:
            assert Decimal(str(r["amount"])) >= 0
    w.close()


def test_v16_order_identity_reservation_released_only_when_terminal(tmp_path):
    w = world(tmp_path)
    proposal = sell("s1", "10")
    reserve(w, proposal)
    _execute(w, proposal, fill_on_submit=False)
    identity = [
        r for r in w.store.active_reservations()
        if r.proposal_id == "s1" and r.kind is ReservationKind.ORDER_IDENTITY
    ]
    assert identity, "a live order keeps its identity reservation"
    w.close()


def test_FINDING_any_live_order_double_counts_its_own_reservation(tmp_path):
    """Simpler and broader than the partial-fill case: an ordinary live order with
    no fill at all is counted twice, so a 10-share order costs 20 of capacity."""
    from opaca.orchestration.context import build_policy_context
    from opaca.policy.engine import sell_reservations

    w = world(tmp_path, qty="100")
    proposal = sell("s1", "10")
    reserve(w, proposal)
    result = _execute(w, proposal, fill_on_submit=False)
    assert result.state is ExecutionState.SUBMITTED
    assert active_sell_qty(w.store) == Decimal("10")
    assert reconcile(w.store, w.read(), now=DEFAULT_NOW).status is (
        ReconciliationStatus.RECONCILED
    )
    context, _ = build_policy_context(w.store, now=DEFAULT_NOW, prices=DEFAULT_PRICES)
    reserved, _u = sell_reservations(context.unresolved_orders)
    counted = reserved.get("SGOV", Decimal("0"))
    honest = sell("s2", "90")
    _, out = reserve(w, honest)
    w.close()
    assert counted == Decimal("20"), f"probe assumption: counted {counted}"
    assert out.is_auto is False, "probe assumption"
    pytest.fail(
        "FINDING P1-2 (general case): one live 10-share sell is counted as 20 of "
        "encumbrance (the reservation plus the broker order it created), so an honest "
        "90-share sell against the untouched 90 shares is REJECTed. Every open order "
        "costs twice its size in capacity for as long as it lives."
    )
