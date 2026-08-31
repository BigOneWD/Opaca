"""Offline paper execution lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from opaca.domain.models import AuthorityResult, BrokerCashState, Proposal, Side
from opaca.execution.errors import ExecutionBlockedError, IllegalTransitionError
from opaca.execution.gateway import (
    FakePaperExecutionGateway,
    PaperMutatingGateway,
    PaperOrderRequest,
    assert_paper_execution_gateway,
)
from opaca.execution.service import (
    ExecutionResult,
    cancel_remaining,
    execute_reserved_proposal,
    grant_human_approval,
    recover_open_executions,
    recover_proposal,
)
from opaca.execution.states import validate_transition
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.codec import dump_date, dump_decimal
from opaca.persistence.types import (
    AuditEventType,
    ExecutionOrderRecord,
    ExecutionState,
    ReservationKind,
)
from opaca.policy.client_order_id import deterministic_client_order_id
from opaca.treasury.liquidity import compute_liquidity

from tests.execution_helpers import (
    PaperWorld,
    active_capacity,
    buy_one,
    make_world,
    reserve_proposal,
    sell_qty,
)
from tests.helpers import DEFAULT_NOW, DEFAULT_PRICES, make_order, make_proposal


def _execute(
    world: PaperWorld,
    proposal: Proposal,
    mutate: PaperMutatingGateway,
    now: datetime = DEFAULT_NOW,
) -> ExecutionResult:
    return execute_reserved_proposal(
        world.store,
        world.read(),
        mutate,
        proposal,
        now=now,
        prices=DEFAULT_PRICES,
    )


class TestGateway:
    def test_paper_only_and_narrow_surface(self) -> None:
        from opaca.broker.gateway import PAPER_ENDPOINT

        gateway = FakePaperExecutionGateway()
        assert gateway.endpoint.startswith(PAPER_ENDPOINT)
        assert_paper_execution_gateway(gateway)
        assert callable(gateway.submit_order)
        assert callable(gateway.cancel_order_by_id)
        assert not callable(getattr(gateway, "replace_order", None))
        assert not callable(getattr(gateway, "close_position", None))
        assert not callable(getattr(gateway, "request", None))


class TestStateMachine:
    def test_illegal_transition_fails_closed(self) -> None:
        with pytest.raises(IllegalTransitionError):
            validate_transition(
                ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION, ExecutionState.SUBMITTING
            )
        with pytest.raises(IllegalTransitionError):
            validate_transition(ExecutionState.FILLED, ExecutionState.SUBMITTED)
        validate_transition(ExecutionState.READY, ExecutionState.SUBMITTING)
        validate_transition(ExecutionState.SUBMITTING, ExecutionState.SUBMITTED)
        validate_transition(ExecutionState.SUBMITTING, ExecutionState.NOT_SUBMITTED)
        with pytest.raises(IllegalTransitionError):
            validate_transition(
                ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION, ExecutionState.NOT_SUBMITTED
            )


class TestIdempotency:
    def test_exact_retry_same_client_order_id(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("retry-buy")
        _version, outcome = reserve_proposal(world, proposal)
        assert outcome.is_auto is True
        cid = proposal.legs[0].client_order_id
        assert cid == deterministic_client_order_id("retry-buy", 0)
        mutate = world.mutate()
        first = _execute(world, proposal, mutate)
        assert first.submitted is True
        assert first.state is ExecutionState.FILLED
        assert mutate.submit_calls == 1
        second = _execute(world, proposal, mutate)
        assert second.recovered is True
        assert second.submitted is False
        assert mutate.submit_calls == 1
        assert second.client_order_ids == (cid,)

    def test_duplicate_submit_prevention(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("dup-buy")
        reserve_proposal(world, proposal)
        mutate = world.mutate()
        _execute(world, proposal, mutate)
        _execute(world, proposal, mutate)
        assert mutate.submit_calls == 1
        assert len([k for k in world.orders if k]) == 1


class TestUnknownRecovery:
    def test_timeout_after_broker_accept_is_unknown_not_failed(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("to-ack")
        reserve_proposal(world, proposal)
        mutate = world.mutate(timeout_after_accept=True)
        result = _execute(world, proposal, mutate)
        assert result.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
        assert result.submitted is True
        assert result.reservation_active is True
        assert mutate.submit_calls == 1
        recovered = recover_proposal(
            world.store, world.read(), proposal.proposal_id, now=DEFAULT_NOW
        )
        assert recovered.state is ExecutionState.FILLED
        assert recovered.recovered is True
        assert mutate.submit_calls == 1

    def test_timeout_before_accept_retains_reservation_never_resubmits(
        self, tmp_path: Path
    ) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("to-pre")
        reserve_proposal(world, proposal)
        mutate = world.mutate(timeout_before_accept=True)
        result = _execute(world, proposal, mutate)
        assert result.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
        assert active_capacity(world.store, proposal.proposal_id) >= 1
        again = _execute(world, proposal, mutate)
        assert again.recovered is True
        assert mutate.submit_calls == 1
        assert again.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION

    def test_lookup_recovery_by_client_order_id(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("lookup")
        reserve_proposal(world, proposal)
        cid = proposal.legs[0].client_order_id
        now = DEFAULT_NOW
        with world.store.begin_immediate() as conn:
            world.store.insert_execution_order(
                ExecutionOrderRecord(
                    client_order_id=cid,
                    proposal_id=proposal.proposal_id,
                    leg_index=0,
                    symbol="SGOV",
                    side=Side.BUY,
                    quantity=Decimal("1"),
                    filled_quantity=Decimal("0"),
                    remaining_quantity=Decimal("1"),
                    state=ExecutionState.SUBMITTING,
                    broker_order_id=None,
                    last_broker_status=None,
                    filled_avg_price=None,
                    reference_price=DEFAULT_PRICES["SGOV"],
                    reconciled_filled_quantity=Decimal("0"),
                    settled_proceeds=Decimal("0"),
                    created_at=now,
                    updated_at=now,
                ),
                conn=conn,
            )
        world.orders[cid] = {
            "id": "broker-recovered",
            "client_order_id": cid,
            "symbol": "SGOV",
            "side": "buy",
            "status": "filled",
            "qty": "1",
            "filled_qty": "1",
            "filled_avg_price": format(DEFAULT_PRICES["SGOV"], "f"),
        }
        recovered = recover_proposal(world.store, world.read(), proposal.proposal_id, now=now)
        assert recovered.state is ExecutionState.FILLED
        assert store_audit(world, AuditEventType.ORDER_RECOVERED)


def store_audit(world: PaperWorld, event_type: AuditEventType) -> bool:
    return bool(world.store.list_audit(event_type=event_type))


class TestBrokerOutcomes:
    def test_broker_rejection_releases_reservation(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("reject-buy")
        reserve_proposal(world, proposal)
        mutate = world.mutate(reject_reason="rejected by broker")
        result = _execute(world, proposal, mutate)
        assert result.state is ExecutionState.REJECTED
        assert active_capacity(world.store, proposal.proposal_id) == 0
        assert world.store.list_audit(
            event_type=AuditEventType.ORDER_REJECTED, proposal_id=proposal.proposal_id
        )

    def test_partial_fill_resizes_sell_reservation(self, tmp_path: Path) -> None:
        world = make_world(tmp_path, qty="100")
        proposal = sell_qty("sell-partial", "100")
        outcome = reserve_proposal(world, proposal)[1]
        assert outcome.is_auto is True
        mutate = world.mutate(partial_fill_qty=Decimal("40"))
        result = _execute(world, proposal, mutate)
        assert result.state is ExecutionState.PARTIALLY_FILLED
        assert result.filled_quantity == Decimal("40")
        assert result.remaining_quantity == Decimal("60")
        reserved = [
            item
            for item in world.store.active_reservations()
            if item.proposal_id == proposal.proposal_id
            and item.kind is ReservationKind.SELL_QUANTITY
        ]
        assert len(reserved) == 1
        assert reserved[0].quantity == Decimal("60")
        other = sell_qty("sell-other", "60")
        recon_v, blocked = reserve_proposal(world, other)
        del recon_v
        assert blocked.is_auto is False
        assert blocked.authority_result is AuthorityResult.REJECT

    def test_full_fill_releases_reservation(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("full-buy")
        reserve_proposal(world, proposal)
        result = _execute(world, proposal, world.mutate())
        assert result.state is ExecutionState.FILLED
        assert active_capacity(world.store, proposal.proposal_id) == 0
        assert world.store.list_audit(
            event_type=AuditEventType.FULL_FILL, proposal_id=proposal.proposal_id
        )

    def test_cancel_remaining_after_partial(self, tmp_path: Path) -> None:
        world = make_world(tmp_path, qty="100")
        proposal = sell_qty("sell-cancel", "100")
        reserve_proposal(world, proposal)
        mutate = world.mutate(partial_fill_qty=Decimal("40"))
        result = _execute(world, proposal, mutate)
        assert result.state is ExecutionState.PARTIALLY_FILLED
        cancelled = cancel_remaining(
            world.store,
            world.read(),
            mutate,
            proposal.legs[0].client_order_id,
            now=DEFAULT_NOW,
        )
        assert cancelled.state is ExecutionState.CANCELLED
        assert active_capacity(world.store, proposal.proposal_id) == 0


class TestCrashRecovery:
    def test_a_reservation_without_submit_can_still_execute(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("crash-a")
        reserve_proposal(world, proposal)
        assert recover_open_executions(world.store, world.read(), now=DEFAULT_NOW) == ()
        mutate = world.mutate()
        result = _execute(world, proposal, mutate)
        assert result.state is ExecutionState.FILLED
        assert mutate.submit_calls == 1

    def test_b_submit_attempted_response_lost(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("crash-b")
        reserve_proposal(world, proposal)
        mutate = world.mutate(timeout_after_accept=True)
        result = _execute(world, proposal, mutate)
        assert result.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
        recovered = recover_open_executions(world.store, world.read(), now=DEFAULT_NOW)
        assert recovered[0].state is ExecutionState.FILLED
        assert mutate.submit_calls == 1

    def test_c_broker_accepted_local_ack_missing(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("crash-c")
        reserve_proposal(world, proposal)
        cid = proposal.legs[0].client_order_id
        now = DEFAULT_NOW
        with world.store.begin_immediate() as conn:
            world.store.insert_execution_order(
                ExecutionOrderRecord(
                    client_order_id=cid,
                    proposal_id=proposal.proposal_id,
                    leg_index=0,
                    symbol="SGOV",
                    side=Side.BUY,
                    quantity=Decimal("1"),
                    filled_quantity=Decimal("0"),
                    remaining_quantity=Decimal("1"),
                    state=ExecutionState.SUBMITTING,
                    broker_order_id=None,
                    last_broker_status=None,
                    filled_avg_price=None,
                    reference_price=DEFAULT_PRICES["SGOV"],
                    reconciled_filled_quantity=Decimal("0"),
                    settled_proceeds=Decimal("0"),
                    created_at=now,
                    updated_at=now,
                ),
                conn=conn,
            )
        world.orders[cid] = {
            "id": "ack-missing",
            "client_order_id": cid,
            "symbol": "SGOV",
            "side": "buy",
            "status": "new",
            "qty": "1",
            "filled_qty": "0",
            "filled_avg_price": None,
        }
        recovered = recover_proposal(world.store, world.read(), proposal.proposal_id, now=now)
        assert recovered.state is ExecutionState.SUBMITTED
        mutate = world.mutate()
        again = _execute(world, proposal, mutate)
        assert again.recovered is True
        assert mutate.submit_calls == 0

    def test_d_partial_fill_not_persisted_recovers(self, tmp_path: Path) -> None:
        world = make_world(tmp_path, qty="100")
        proposal = sell_qty("crash-d", "100")
        reserve_proposal(world, proposal)
        cid = proposal.legs[0].client_order_id
        now = DEFAULT_NOW
        with world.store.begin_immediate() as conn:
            world.store.insert_execution_order(
                ExecutionOrderRecord(
                    client_order_id=cid,
                    proposal_id=proposal.proposal_id,
                    leg_index=0,
                    symbol="SGOV",
                    side=Side.SELL,
                    quantity=Decimal("100"),
                    filled_quantity=Decimal("0"),
                    remaining_quantity=Decimal("100"),
                    state=ExecutionState.SUBMITTED,
                    broker_order_id="b1",
                    last_broker_status="new",
                    filled_avg_price=None,
                    reference_price=DEFAULT_PRICES["SGOV"],
                    reconciled_filled_quantity=Decimal("0"),
                    settled_proceeds=Decimal("0"),
                    created_at=now,
                    updated_at=now,
                ),
                conn=conn,
            )
        world.orders[cid] = {
            "id": "b1",
            "client_order_id": cid,
            "symbol": "SGOV",
            "side": "sell",
            "status": "partially_filled",
            "qty": "100",
            "filled_qty": "40",
            "filled_avg_price": format(DEFAULT_PRICES["SGOV"], "f"),
        }
        recovered = recover_proposal(world.store, world.read(), proposal.proposal_id, now=now)
        assert recovered.state is ExecutionState.PARTIALLY_FILLED
        assert recovered.filled_quantity == Decimal("40")

    def test_e_fill_complete_before_local_update(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("crash-e")
        reserve_proposal(world, proposal)
        cid = proposal.legs[0].client_order_id
        now = DEFAULT_NOW
        with world.store.begin_immediate() as conn:
            world.store.insert_execution_order(
                ExecutionOrderRecord(
                    client_order_id=cid,
                    proposal_id=proposal.proposal_id,
                    leg_index=0,
                    symbol="SGOV",
                    side=Side.BUY,
                    quantity=Decimal("1"),
                    filled_quantity=Decimal("0"),
                    remaining_quantity=Decimal("1"),
                    state=ExecutionState.SUBMITTING,
                    broker_order_id=None,
                    last_broker_status=None,
                    filled_avg_price=None,
                    reference_price=DEFAULT_PRICES["SGOV"],
                    reconciled_filled_quantity=Decimal("0"),
                    settled_proceeds=Decimal("0"),
                    created_at=now,
                    updated_at=now,
                ),
                conn=conn,
            )
        world.orders[cid] = {
            "id": "filled-first",
            "client_order_id": cid,
            "symbol": "SGOV",
            "side": "buy",
            "status": "filled",
            "qty": "1",
            "filled_qty": "1",
            "filled_avg_price": format(DEFAULT_PRICES["SGOV"], "f"),
        }
        recovered = recover_proposal(world.store, world.read(), proposal.proposal_id, now=now)
        assert recovered.state is ExecutionState.FILLED


class TestRevalidation:
    def test_stale_snapshot_does_not_submit(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("stale")
        reserve_proposal(world, proposal)
        from opaca.reconciliation.service import reconcile as real_reconcile

        frozen = real_reconcile(world.store, world.read(), now=DEFAULT_NOW)
        later = DEFAULT_NOW + timedelta(seconds=120)
        mutate = world.mutate()
        with patch("opaca.execution.service.reconcile", return_value=frozen):
            result = execute_reserved_proposal(
                world.store,
                world.read(),
                mutate,
                proposal,
                now=later,
                prices=DEFAULT_PRICES,
            )
        assert result.blocked is True
        assert mutate.submit_calls == 0

    def test_historical_auto_replay_is_not_eligibility(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("hist-auto")
        version, first = reserve_proposal(world, proposal)
        replay = evaluate_and_reserve(
            world.store,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=version,
        )
        assert first.is_auto is True
        assert replay.is_auto is False
        assert replay.idempotent_replay is True
        result = _execute(world, proposal, world.mutate())
        assert result.blocked is False
        assert result.state is ExecutionState.FILLED

    def test_kill_switch_after_reservation_before_submit(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("kill")
        reserve_proposal(world, proposal)
        world.store.set_kill_switch(True, now=DEFAULT_NOW)
        mutate = world.mutate()
        result = _execute(world, proposal, mutate)
        assert result.blocked is True
        assert mutate.submit_calls == 0
        assert active_capacity(world.store, proposal.proposal_id) >= 1

    def test_cash_change_before_submit(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("cash-chg")
        reserve_proposal(world, proposal)
        world.account["cash"] = "0.00"
        mutate = world.mutate()
        result = _execute(world, proposal, mutate)
        assert result.blocked is True
        assert mutate.submit_calls == 0

    def test_obligation_change_before_submit(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("obl-chg")
        reserve_proposal(world, proposal)
        world.store._conn.execute(
            "INSERT INTO obligations(obligation_id, name, amount, due_date, seeded) "
            "VALUES (?, ?, ?, ?, 0)",
            ("extra-tax", "extra", dump_decimal(Decimal("90000")), dump_date(DEFAULT_NOW.date())),
        )
        mutate = world.mutate()
        result = _execute(world, proposal, mutate)
        assert result.blocked is True
        assert mutate.submit_calls == 0


class TestApproval:
    def test_granted_unexpired_approval_can_submit(self, tmp_path: Path) -> None:
        world = make_world(tmp_path, qty="400")
        proposal = sell_qty("cfo-ok", "300")
        reserved = reserve_proposal(world, proposal)[1]
        assert reserved.authority_result is AuthorityResult.APPROVAL_REQUIRED
        grant_human_approval(world.store, proposal, now=DEFAULT_NOW)
        mutate = world.mutate()
        result = _execute(world, proposal, mutate)
        assert result.blocked is False
        assert result.submitted is True
        assert mutate.submit_calls == 1

    def test_approval_expiry_blocks_submit(self, tmp_path: Path) -> None:
        world = make_world(tmp_path, qty="400")
        proposal = sell_qty("needs-cfo", "300")
        _version, outcome = reserve_proposal(world, proposal)
        assert outcome.authority_result is AuthorityResult.APPROVAL_REQUIRED
        grant_human_approval(world.store, proposal, now=DEFAULT_NOW)
        expired = DEFAULT_NOW + timedelta(seconds=301)
        mutate = world.mutate()
        result = execute_reserved_proposal(
            world.store,
            world.read(),
            mutate,
            proposal,
            now=expired,
            prices=DEFAULT_PRICES,
        )
        assert result.blocked is True
        assert mutate.submit_calls == 0

    def test_stale_approval_cannot_bypass_changed_state(self, tmp_path: Path) -> None:
        world = make_world(tmp_path, qty="400")
        proposal = sell_qty("cfo-stale", "300")
        reserve_proposal(world, proposal)
        grant_human_approval(world.store, proposal, now=DEFAULT_NOW)
        world.positions.clear()
        mutate = world.mutate()
        result = _execute(world, proposal, mutate)
        assert result.blocked is True
        assert mutate.submit_calls == 0


class TestConcurrency:
    def test_sell_reservation_concurrency(self, tmp_path: Path) -> None:
        world = make_world(tmp_path, qty="100")
        first = sell_qty("sell-a", "60")
        second = sell_qty("sell-b", "60")
        a = reserve_proposal(world, first)[1]
        b = reserve_proposal(world, second)[1]
        assert a.is_auto is True
        assert b.is_auto is False
        mutate = world.mutate()
        result = _execute(world, first, mutate)
        assert result.state is ExecutionState.FILLED
        blocked = _execute(world, second, mutate)
        assert blocked.blocked is True or blocked.submitted is False

    def test_buy_cash_concurrency(self, tmp_path: Path) -> None:
        world = make_world(tmp_path, qty="0")
        one = make_proposal(
            "cash-a",
            [make_order("cash-a", 0, "SGOV", Side.BUY, "150", DEFAULT_PRICES["SGOV"])],
        )
        two = make_proposal(
            "cash-b",
            [make_order("cash-b", 0, "SGOV", Side.BUY, "150", DEFAULT_PRICES["SGOV"])],
        )
        assert reserve_proposal(world, one)[1].is_auto is True
        assert reserve_proposal(world, two)[1].is_auto is False
        mutate = world.mutate()
        filled = _execute(world, one, mutate)
        assert filled.state is ExecutionState.FILLED
        blocked = _execute(world, two, mutate)
        assert blocked.blocked is True
        assert mutate.submit_calls == 1


def _cash_state(cash: Decimal, as_of: datetime) -> BrokerCashState:
    return BrokerCashState(
        cash=cash,
        buying_power=Decimal("400000"),
        non_marginable_buying_power=cash,
        multiplier=Decimal("4"),
        as_of=as_of,
    )


class TestSettlement:
    def test_t1_sell_does_not_increase_protected_liquidity(self, tmp_path: Path) -> None:
        friday = datetime(2026, 8, 28, 14, 30, tzinfo=UTC)
        world = make_world(tmp_path, qty="10", now=friday)
        proposal = sell_qty("t1-sell", "10")
        outcome = reserve_proposal(world, proposal, now=friday)[1]
        assert outcome.is_auto is True
        cash_before = Decimal(str(world.account["cash"]))
        scenario = world.store.get_scenario()
        assert scenario is not None
        before = compute_liquidity(
            _cash_state(cash_before, friday),
            scenario.obligations,
            (),
            scenario.operating_reserve,
            friday.date(),
        )
        result = execute_reserved_proposal(
            world.store,
            world.read(),
            world.mutate(),
            proposal,
            now=friday,
            prices=DEFAULT_PRICES,
        )
        assert result.state is ExecutionState.FILLED
        cash_after = Decimal(str(world.account["cash"]))
        assert cash_after > cash_before
        events = world.store.load_settlement_events()
        assert events
        assert events[0].settlement_date.isoformat() == "2026-08-31"
        after = compute_liquidity(
            _cash_state(cash_after, friday),
            scenario.obligations,
            events,
            scenario.operating_reserve,
            friday.date(),
        )
        assert after.unsettled_total > 0
        assert after.protected_liquidity == before.protected_liquidity
        assert after.investable_cash == before.investable_cash
        monday_as_of = datetime(2026, 8, 31, 14, 30, tzinfo=UTC)
        monday = compute_liquidity(
            _cash_state(cash_after, monday_as_of),
            scenario.obligations,
            events,
            scenario.operating_reserve,
            monday_as_of.date(),
        )
        assert monday.unsettled_total == Decimal("0")
        assert monday.investable_cash > after.investable_cash

    def test_weekend_holiday_settlement(self, tmp_path: Path) -> None:
        from opaca.calendar.us_trading_calendar import US_TRADING_CALENDAR

        friday = datetime(2026, 9, 4, 14, 30, tzinfo=UTC)
        world = make_world(tmp_path, qty="10", now=friday)
        proposal = sell_qty("hol-sell", "10")
        assert reserve_proposal(world, proposal, now=friday)[1].is_auto is True
        result = execute_reserved_proposal(
            world.store,
            world.read(),
            world.mutate(),
            proposal,
            now=friday,
            prices=DEFAULT_PRICES,
        )
        assert result.state is ExecutionState.FILLED
        events = world.store.load_settlement_events()
        assert events[0].settlement_date == US_TRADING_CALENDAR.settlement_date(friday.date())
        assert events[0].settlement_date.isoformat() == "2026-09-08"


class TestSqlite:
    def test_rollback_leaves_no_execution_row(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("boom")
        reserve_proposal(world, proposal)
        original = world.store.insert_execution_order

        def exploding(record: ExecutionOrderRecord, conn: object) -> None:
            original(record, conn)  # type: ignore[arg-type]
            raise RuntimeError("injected failure")

        world.store.insert_execution_order = exploding  # type: ignore[method-assign]
        mutate = world.mutate()
        with pytest.raises(RuntimeError):
            _execute(world, proposal, mutate)
        assert world.store.list_execution_orders(proposal_id=proposal.proposal_id) == ()
        assert mutate.submit_calls == 0
        assert active_capacity(world.store, proposal.proposal_id) >= 1


class TestRepeatedReconcile:
    def test_changing_broker_views_of_partial_fill(self, tmp_path: Path) -> None:
        world = make_world(tmp_path, qty="100")
        proposal = sell_qty("view-chg", "100")
        reserve_proposal(world, proposal)
        mutate = world.mutate(partial_fill_qty=Decimal("40"))
        result = _execute(world, proposal, mutate)
        assert result.state is ExecutionState.PARTIALLY_FILLED
        from opaca.persistence.types import ReconciliationStatus
        from opaca.reconciliation.service import reconcile

        first = reconcile(world.store, world.read(), now=DEFAULT_NOW)
        cid = proposal.legs[0].client_order_id
        world.orders[cid] = {
            **world.orders[cid],
            "status": "filled",
            "filled_qty": "100",
        }
        world.open_orders.clear()
        recovered = recover_proposal(
            world.store, world.read(), proposal.proposal_id, now=DEFAULT_NOW
        )
        assert recovered.state is ExecutionState.FILLED
        world.positions.clear()
        second = reconcile(world.store, world.read(), now=DEFAULT_NOW + timedelta(seconds=1))
        assert first.status is ReconciliationStatus.RECONCILED
        assert second.status is ReconciliationStatus.RECONCILED


class TestRequestIdentity:
    def test_paper_order_request_rejects_live_semantics(self) -> None:
        with pytest.raises(ValueError):
            PaperOrderRequest(
                symbol="SGOV",
                side=Side.BUY,
                quantity=Decimal("1"),
                client_order_id=deterministic_client_order_id("x", 0),
                time_in_force="gtc",
            )
        with pytest.raises(ValueError):
            PaperOrderRequest(
                symbol="SGOV",
                side=Side.BUY,
                quantity=Decimal("1"),
                client_order_id=deterministic_client_order_id("x", 0),
                order_type="stop",
            )
        with pytest.raises(ValueError):
            PaperOrderRequest(
                symbol="SGOV",
                side=Side.BUY,
                quantity=Decimal("1"),
                client_order_id=deterministic_client_order_id("x", 0),
                order_type="limit",
            )
        bounded = PaperOrderRequest(
            symbol="SGOV",
            side=Side.BUY,
            quantity=Decimal("1"),
            client_order_id=deterministic_client_order_id("x", 0),
            order_type="limit",
            limit_price=Decimal("100.80"),
        )
        assert bounded.order_type == "limit"
        assert bounded.limit_price == Decimal("100.80")


class TestUnknownCancel:
    def test_unknown_cannot_be_cancelled(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = buy_one("no-cancel")
        reserve_proposal(world, proposal)
        mutate = world.mutate(timeout_before_accept=True)
        result = _execute(world, proposal, mutate)
        assert result.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
        with pytest.raises(ExecutionBlockedError):
            cancel_remaining(
                world.store,
                world.read(),
                mutate,
                proposal.legs[0].client_order_id,
                now=DEFAULT_NOW,
            )


def test_grant_requires_existing_proposal(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    proposal = buy_one("no-such")
    with pytest.raises(ExecutionBlockedError):
        grant_human_approval(world.store, proposal, now=DEFAULT_NOW)
