"""Phase 3 red-team remediations: double-count, zero-delta, stranded legs, kill switch."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from opaca.domain.models import (
    AuthorityDecision,
    AuthorityResult,
    OrderState,
    Position,
    Proposal,
    Side,
)
from opaca.execution.gateway import PaperMutatingGateway
from opaca.execution.service import ExecutionResult, execute_reserved_proposal, recover_proposal
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.types import (
    AuditEventType,
    ExecutionOrderRecord,
    ExecutionState,
    PersistedSnapshot,
    ReconciliationStatus,
    ReservationKind,
    UnknownOrderRecord,
)
from opaca.policy.decision import decide as real_decide
from opaca.policy.engine import PolicyContext
from opaca.reconciliation.service import compare_state, reconcile

from tests.execution_helpers import (
    PaperWorld,
    active_capacity,
    bindings_for_proposal,
    buy_one,
    freeze_submit_clock,
    make_world,
    reserve_proposal,
    sell_legs,
    sell_qty,
)
from tests.helpers import DEFAULT_NOW, DEFAULT_PRICES, phase1_broker_cash
from tests.market_helpers import market_data_from_bindings
from tests.state_helpers import order_payload


def _execute(
    world: PaperWorld,
    proposal: Proposal,
    mutate: PaperMutatingGateway,
    now: datetime = DEFAULT_NOW,
) -> ExecutionResult:
    bindings = bindings_for_proposal(proposal, now=now)
    with freeze_submit_clock(now):
        return execute_reserved_proposal(
            world.store,
            world.read(),
            mutate,
            proposal,
            now=now,
            prices=DEFAULT_PRICES,
            price_bindings=bindings,
            market_data=market_data_from_bindings(bindings),
        )


def _position(qty: str, available: str | None = None) -> Position:
    quantity = Decimal(qty)
    avail = Decimal(available) if available is not None else quantity
    return Position(
        symbol="SGOV",
        quantity=quantity,
        quantity_available=avail,
        market_value=quantity * DEFAULT_PRICES["SGOV"],
    )


def _snapshot(positions: tuple[Position, ...]) -> PersistedSnapshot:
    return PersistedSnapshot(
        snapshot_id=1,
        version=1,
        broker=phase1_broker_cash(),
        positions=positions,
        assets=(),
        orders=(),
        reconciliation_status=ReconciliationStatus.RECONCILED,
        captured_at=DEFAULT_NOW,
        diagnostics="{}",
    )


def _fill_record(
    *,
    side: Side,
    filled: str,
    reconciled: str = "0",
    quantity: str = "10",
) -> ExecutionOrderRecord:
    qty = Decimal(quantity)
    filled_qty = Decimal(filled)
    remaining = qty - filled_qty
    return ExecutionOrderRecord(
        client_order_id="opaca-explained",
        proposal_id="explained",
        leg_index=0,
        symbol="SGOV",
        side=side,
        quantity=qty,
        filled_quantity=filled_qty,
        remaining_quantity=remaining,
        state=ExecutionState.FILLED if remaining == 0 else ExecutionState.PARTIALLY_FILLED,
        broker_order_id="broker-explained",
        last_broker_status="filled",
        filled_avg_price=DEFAULT_PRICES["SGOV"],
        reference_price=DEFAULT_PRICES["SGOV"],
        reconciled_filled_quantity=Decimal(reconciled),
        settled_proceeds=Decimal("0"),
        created_at=DEFAULT_NOW,
        updated_at=DEFAULT_NOW,
    )


def _compare(
    *,
    current: str,
    previous: str,
    execution_orders: tuple[ExecutionOrderRecord, ...] = (),
) -> tuple[ReconciliationStatus, list[str]]:
    return compare_state(
        broker=phase1_broker_cash(),
        positions=(_position(current),),
        orders=(),
        previous=_snapshot((_position(previous),)),
        reservations=(),
        unknown_orders=(),
        settlement_events=(),
        as_of=date(2026, 9, 1),
        execution_orders=execution_orders,
    )


class TestDoubleCount:
    def test_opaca_reservation_and_matching_broker_order_counted_once(self, tmp_path: Path) -> None:
        world = make_world(tmp_path, qty="100")
        first = sell_qty("open-10", "10")
        assert reserve_proposal(world, first)[1].is_auto is True
        cid = first.legs[0].client_order_id
        world.open_orders.append(order_payload(cid, qty="10"))
        world.positions[0]["qty_available"] = "90"
        later = sell_qty("later-90", "90")
        outcome = reserve_proposal(world, later)[1]
        assert outcome.is_auto is True

    def test_external_broker_order_counted_independently(self, tmp_path: Path) -> None:
        world = make_world(tmp_path, qty="100")
        local = sell_qty("local-10", "10")
        assert reserve_proposal(world, local)[1].is_auto is True
        with world.store.begin_immediate() as conn:
            world.store.upsert_unknown_order(
                UnknownOrderRecord(
                    client_order_id="external-sell",
                    proposal_id="external-prop",
                    symbol="SGOV",
                    side="SELL",
                    quantity=Decimal("10"),
                    filled_quantity=Decimal("0"),
                    state=OrderState.NEW.value,
                    last_lookup_at=None,
                    created_at=DEFAULT_NOW,
                ),
                conn=conn,
            )
        world.open_orders.append(order_payload("external-sell", qty="10"))
        world.positions[0]["qty_available"] = "90"
        assert reserve_proposal(world, sell_qty("need-90", "90"))[1].is_auto is False
        assert reserve_proposal(world, sell_qty("need-80", "80"))[1].is_auto is True

    def test_ambiguous_identity_fails_closed(self, tmp_path: Path) -> None:
        world = make_world(tmp_path, qty="100")
        local = sell_qty("amb-local", "10")
        assert reserve_proposal(world, local)[1].is_auto is True
        world.open_orders.append(order_payload("not-the-local-id", qty="10"))
        world.positions[0]["qty_available"] = "90"
        recon = reconcile(world.store, world.read(), now=DEFAULT_NOW)
        assert recon.status is not ReconciliationStatus.RECONCILED
        later = evaluate_and_reserve(
            world.store,
            sell_qty("amb-90", "90"),
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=recon.snapshot.version if recon.snapshot else None,
        )
        assert later.is_auto is False
        assert later.authority_result is not AuthorityResult.AUTO

    def test_unknown_identity_correlation_fails_closed(self, tmp_path: Path) -> None:
        world = make_world(tmp_path, qty="100")
        with world.store.begin_immediate() as conn:
            world.store.upsert_unknown_order(
                UnknownOrderRecord(
                    client_order_id="opaca-unknown-same",
                    proposal_id="unknown-prop",
                    symbol="SGOV",
                    side="SELL",
                    quantity=None,
                    filled_quantity=None,
                    state="UNKNOWN",
                    last_lookup_at=None,
                    created_at=DEFAULT_NOW,
                ),
                conn=conn,
            )
        world.open_orders.append(order_payload("opaca-unknown-same", qty="10"))
        world.positions[0]["qty_available"] = "90"
        recon = reconcile(world.store, world.read(), now=DEFAULT_NOW)
        assert recon.status is not ReconciliationStatus.RECONCILED
        later = evaluate_and_reserve(
            world.store,
            sell_qty("after-unknown", "90"),
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=recon.snapshot.version if recon.snapshot else None,
        )
        assert later.is_auto is False

    def test_partial_fill_with_matching_reservation_counted_once(self, tmp_path: Path) -> None:
        world = make_world(tmp_path, qty="100")
        first = sell_qty("partial-match", "10")
        reserve_proposal(world, first)
        mutate = world.mutate(partial_fill_qty=Decimal("4"))
        result = _execute(world, first, mutate)
        assert result.state is ExecutionState.PARTIALLY_FILLED
        reserved = [
            item
            for item in world.store.active_reservations()
            if item.proposal_id == first.proposal_id and item.kind is ReservationKind.SELL_QUANTITY
        ]
        assert len(reserved) == 1
        assert reserved[0].quantity == Decimal("6")
        later = sell_qty("after-partial", "90")
        outcome = reserve_proposal(world, later)[1]
        assert outcome.is_auto is True


class TestZeroDeltaRecon:
    def test_true_no_change_is_reconciled(self) -> None:
        status, _reasons = _compare(current="100", previous="100")
        assert status is ReconciliationStatus.RECONCILED

    def test_known_local_fill_reflected_is_reconciled(self) -> None:
        status, _reasons = _compare(
            current="90",
            previous="100",
            execution_orders=(_fill_record(side=Side.SELL, filled="10"),),
        )
        assert status is ReconciliationStatus.RECONCILED

    def test_hidden_local_fill_is_not_reconciled(self) -> None:
        status, reasons = _compare(
            current="100",
            previous="100",
            execution_orders=(_fill_record(side=Side.SELL, filled="10"),),
        )
        assert status is ReconciliationStatus.DRIFT_DETECTED
        assert any("not reflected" in item for item in reasons)

    def test_equal_opposite_external_movement_is_not_reconciled(self) -> None:
        status, reasons = _compare(
            current="100",
            previous="100",
            execution_orders=(_fill_record(side=Side.BUY, filled="10"),),
        )
        assert status is ReconciliationStatus.DRIFT_DETECTED
        assert any("not reflected" in item for item in reasons)

    def test_duplicate_broker_snapshot_is_reconciled(self) -> None:
        status, _reasons = _compare(
            current="90",
            previous="90",
            execution_orders=(_fill_record(side=Side.SELL, filled="10", reconciled="10"),),
        )
        assert status is ReconciliationStatus.RECONCILED

    def test_partial_fill_progression_is_reconciled(self) -> None:
        status, _reasons = _compare(
            current="60",
            previous="100",
            execution_orders=(_fill_record(side=Side.SELL, filled="40", quantity="100"),),
        )
        assert status is ReconciliationStatus.RECONCILED


class TestStrandedLegs:
    def test_first_leg_rejection_aborts_unsent_later_legs(self, tmp_path: Path) -> None:
        world = make_world(tmp_path, qty="100")
        proposal = sell_legs("first-reject", ("10", "10", "10"))
        reserve_proposal(world, proposal)
        mutate = world.mutate(reject_reason="broker rejected")
        result = _execute(world, proposal, mutate)
        assert result.state is ExecutionState.REJECTED
        assert mutate.submit_calls == 1
        orders = world.store.list_execution_orders(proposal_id=proposal.proposal_id)
        assert orders[0].state is ExecutionState.REJECTED
        assert orders[1].state is ExecutionState.NOT_SUBMITTED
        assert orders[2].state is ExecutionState.NOT_SUBMITTED
        assert active_capacity(world.store, proposal.proposal_id) == 0
        assert world.store.list_audit(
            event_type=AuditEventType.ORDER_NOT_SUBMITTED, proposal_id=proposal.proposal_id
        )

    def test_middle_leg_rejection_aborts_unsent_later_legs(self, tmp_path: Path) -> None:
        world = make_world(tmp_path, qty="100")
        proposal = sell_legs("mid-reject", ("10", "10", "10"))
        reserve_proposal(world, proposal)
        mutate = world.mutate(reject_on_call=2)
        result = _execute(world, proposal, mutate)
        assert result.state is ExecutionState.REJECTED
        assert mutate.submit_calls == 2
        orders = world.store.list_execution_orders(proposal_id=proposal.proposal_id)
        assert orders[0].state is ExecutionState.FILLED
        assert orders[1].state is ExecutionState.REJECTED
        assert orders[2].state is ExecutionState.NOT_SUBMITTED
        assert active_capacity(world.store, proposal.proposal_id) == 0

    def test_final_leg_rejection_has_no_unsent_legs(self, tmp_path: Path) -> None:
        world = make_world(tmp_path, qty="100")
        proposal = sell_legs("final-reject", ("10", "10", "10"))
        reserve_proposal(world, proposal)
        mutate = world.mutate(reject_on_call=3)
        result = _execute(world, proposal, mutate)
        assert result.state is ExecutionState.REJECTED
        assert mutate.submit_calls == 3
        orders = world.store.list_execution_orders(proposal_id=proposal.proposal_id)
        assert orders[0].state is ExecutionState.FILLED
        assert orders[1].state is ExecutionState.FILLED
        assert orders[2].state is ExecutionState.REJECTED
        assert all(item.state is not ExecutionState.NOT_SUBMITTED for item in orders)
        assert active_capacity(world.store, proposal.proposal_id) == 0

    def test_unknown_first_leg_aborts_later_but_retains_reservation(self, tmp_path: Path) -> None:
        world = make_world(tmp_path, qty="100")
        proposal = sell_legs("unknown-first", ("10", "10"))
        reserve_proposal(world, proposal)
        mutate = world.mutate(timeout_before_accept=True)
        result = _execute(world, proposal, mutate)
        assert result.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
        assert mutate.submit_calls == 1
        orders = world.store.list_execution_orders(proposal_id=proposal.proposal_id)
        assert orders[0].state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
        assert orders[1].state is ExecutionState.NOT_SUBMITTED
        assert active_capacity(world.store, proposal.proposal_id) >= 1


class TestFinalKillSwitch:
    def test_a_before_revalidation(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("kill-a")
        reserve_proposal(world, proposal)
        world.store.set_kill_switch(True, now=DEFAULT_NOW)
        mutate = world.mutate()
        result = _execute(world, proposal, mutate)
        assert result.blocked is True
        assert mutate.submit_calls == 0
        assert world.store.list_execution_orders(proposal_id=proposal.proposal_id) == ()
        assert active_capacity(world.store, proposal.proposal_id) >= 1

    def test_b_after_revalidation_before_intent(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("kill-b")
        reserve_proposal(world, proposal)
        mutate = world.mutate()

        def flipping(proposal_arg: Proposal, context: PolicyContext) -> AuthorityDecision:
            world.store.set_kill_switch(True, now=DEFAULT_NOW)
            return real_decide(proposal_arg, context)

        with patch("opaca.execution.service.decide", side_effect=flipping):
            result = _execute(world, proposal, mutate)
        assert result.blocked is True
        assert mutate.submit_calls == 0
        assert world.store.list_execution_orders(proposal_id=proposal.proposal_id) == ()
        assert active_capacity(world.store, proposal.proposal_id) >= 1

    def test_c_after_intent_immediately_before_submit(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("kill-c")
        reserve_proposal(world, proposal)
        real = world.store.kill_switch_active

        def flipped(conn: sqlite3.Connection | None = None) -> bool:
            if world.store.list_execution_orders(proposal_id=proposal.proposal_id):
                return True
            return real(conn=conn)

        world.store.kill_switch_active = flipped  # type: ignore[method-assign]
        mutate = world.mutate()
        result = _execute(world, proposal, mutate)
        assert mutate.submit_calls == 0
        assert result.submitted is False
        assert result.blocked is True
        orders = world.store.list_execution_orders(proposal_id=proposal.proposal_id)
        assert orders
        assert all(item.state is ExecutionState.NOT_SUBMITTED for item in orders)
        assert world.store.list_audit(
            event_type=AuditEventType.ORDER_NOT_SUBMITTED, proposal_id=proposal.proposal_id
        )

    def test_c_restart_after_blocked_submission(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("kill-c-restart")
        reserve_proposal(world, proposal)
        real = world.store.kill_switch_active

        def flipped(conn: sqlite3.Connection | None = None) -> bool:
            if world.store.list_execution_orders(proposal_id=proposal.proposal_id):
                return True
            return real(conn=conn)

        world.store.kill_switch_active = flipped  # type: ignore[method-assign]
        mutate = world.mutate()
        blocked = _execute(world, proposal, mutate)
        assert blocked.submitted is False
        assert mutate.submit_calls == 0
        world.store.kill_switch_active = real  # type: ignore[method-assign]
        again = _execute(world, proposal, mutate)
        assert again.recovered is True
        assert mutate.submit_calls == 0
        assert again.state is ExecutionState.NOT_SUBMITTED

    def test_d_after_broker_mutation_attempted(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("kill-d")
        reserve_proposal(world, proposal)
        mutate = world.mutate(timeout_after_accept=True)
        result = _execute(world, proposal, mutate)
        assert result.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
        assert mutate.submit_calls == 1
        world.store.set_kill_switch(True, now=DEFAULT_NOW)
        recovered = recover_proposal(
            world.store, world.read(), proposal.proposal_id, now=DEFAULT_NOW
        )
        assert recovered.state is ExecutionState.FILLED
        assert mutate.submit_calls == 1
        order = world.store.get_execution_order(proposal.legs[0].client_order_id)
        assert order is not None
        assert order.state is not ExecutionState.NOT_SUBMITTED
