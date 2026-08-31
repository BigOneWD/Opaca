"""Final mutation-boundary quote freshness. Wall clock, not caller now."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from opaca.domain.models import Proposal
from opaca.execution.gateway import PaperMutatingGateway
from opaca.execution.service import ExecutionResult, execute_reserved_proposal, recover_proposal
from opaca.market.binding import BoundExecutionPrice, bind_buy, bind_single_leg_proposal
from opaca.orchestration.reserve import OrchestrationResult, evaluate_and_reserve
from opaca.persistence.types import AuditEventType, ExecutionState
from opaca.reconciliation.service import reconcile

from tests.execution_helpers import PaperWorld, active_capacity, freeze_submit_clock, make_world
from tests.helpers import DEFAULT_NOW
from tests.market_helpers import universe_quotes


def _bound_buy(
    pid: str, *, age_seconds: int = 0
) -> tuple[Proposal, Mapping[str, Decimal], Mapping[str, BoundExecutionPrice]]:
    quotes = universe_quotes(now=DEFAULT_NOW, age_seconds=age_seconds)
    bound = bind_buy(quotes["SGOV"], Decimal("1"), tolerance=Decimal("0"))
    return bind_single_leg_proposal(pid, bound, quotes)


def _reserve_bound(
    world: PaperWorld, pid: str, *, age_seconds: int = 0
) -> tuple[Proposal, Mapping[str, Decimal], Mapping[str, BoundExecutionPrice], OrchestrationResult]:
    proposal, prices, bindings = _bound_buy(pid, age_seconds=age_seconds)
    recon = reconcile(world.store, world.read(), now=DEFAULT_NOW)
    assert recon.snapshot is not None
    outcome = evaluate_and_reserve(
        world.store,
        proposal,
        now=DEFAULT_NOW,
        prices=prices,
        expected_snapshot_version=recon.snapshot.version,
        price_bindings=bindings,
    )
    return proposal, prices, bindings, outcome


def _execute_at(
    world: PaperWorld,
    proposal: Proposal,
    prices: Mapping[str, Decimal],
    bindings: Mapping[str, BoundExecutionPrice],
    mutate: PaperMutatingGateway,
    *,
    persist_now: datetime,
    wall_now: datetime,
) -> ExecutionResult:
    with freeze_submit_clock(wall_now):
        return execute_reserved_proposal(
            world.store,
            world.read(),
            mutate,
            proposal,
            now=persist_now,
            prices=prices,
            price_bindings=bindings,
        )


class TestFinalBoundaryFreshness:
    def test_quote_fresh_at_initial_evaluation(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        _proposal, _prices, _bindings, outcome = _reserve_bound(world, "fresh-eval")
        assert outcome.is_auto is True
        world.store.close()

    def test_fourteen_nine_nine_nine_seconds_allowed(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "age-14999")
        assert outcome.is_auto is True
        mutate = world.mutate()
        wall = DEFAULT_NOW + timedelta(seconds=14, microseconds=999000)
        result = _execute_at(
            world, proposal, prices, bindings, mutate, persist_now=DEFAULT_NOW, wall_now=wall
        )
        assert result.blocked is False
        assert result.submitted is True
        assert mutate.submit_calls == 1
        world.store.close()

    def test_exact_fifteen_seconds_allowed(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "age-15000")
        assert outcome.is_auto is True
        mutate = world.mutate()
        wall = DEFAULT_NOW + timedelta(seconds=15)
        result = _execute_at(
            world, proposal, prices, bindings, mutate, persist_now=DEFAULT_NOW, wall_now=wall
        )
        assert result.blocked is False
        assert result.submitted is True
        assert mutate.submit_calls == 1
        world.store.close()

    def test_fifteen_seconds_plus_one_microsecond_blocked(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "age-15001us")
        assert outcome.is_auto is True
        mutate = world.mutate()
        wall = DEFAULT_NOW + timedelta(seconds=15, microseconds=1)
        result = _execute_at(
            world, proposal, prices, bindings, mutate, persist_now=DEFAULT_NOW, wall_now=wall
        )
        assert mutate.submit_calls == 0
        assert result.submitted is False
        assert result.blocked is True
        orders = world.store.list_execution_orders(proposal_id=proposal.proposal_id)
        assert orders
        assert all(item.state is ExecutionState.NOT_SUBMITTED for item in orders)
        world.store.close()

    def test_sixteen_second_delay_after_validation_submits_zero(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "age-16s", age_seconds=1)
        assert outcome.is_auto is True
        mutate = world.mutate()
        wall = DEFAULT_NOW + timedelta(seconds=16)
        result = _execute_at(
            world, proposal, prices, bindings, mutate, persist_now=DEFAULT_NOW, wall_now=wall
        )
        assert mutate.submit_calls == 0
        assert result.submitted is False
        assert result.blocked is True
        orders = world.store.list_execution_orders(proposal_id=proposal.proposal_id)
        assert all(item.state is ExecutionState.NOT_SUBMITTED for item in orders)
        assert not world.store.list_audit(
            event_type=AuditEventType.ORDER_UNKNOWN, proposal_id=proposal.proposal_id
        )
        world.store.close()

    def test_future_dated_quote_at_final_boundary_blocked(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "future-bound")
        assert outcome.is_auto is True
        mutate = world.mutate()
        wall = DEFAULT_NOW - timedelta(seconds=5)
        result = _execute_at(
            world, proposal, prices, bindings, mutate, persist_now=DEFAULT_NOW, wall_now=wall
        )
        assert mutate.submit_calls == 0
        assert result.blocked is True
        orders = world.store.list_execution_orders(proposal_id=proposal.proposal_id)
        assert all(item.state is ExecutionState.NOT_SUBMITTED for item in orders)
        world.store.close()

    def test_kill_switch_in_the_same_final_window_blocked(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "kill-window")
        assert outcome.is_auto is True
        real = world.store.kill_switch_active

        def flipped(conn: sqlite3.Connection | None = None) -> bool:
            if world.store.list_execution_orders(proposal_id=proposal.proposal_id):
                return True
            return real(conn=conn)

        world.store.kill_switch_active = flipped  # type: ignore[method-assign]
        mutate = world.mutate()
        result = _execute_at(
            world,
            proposal,
            prices,
            bindings,
            mutate,
            persist_now=DEFAULT_NOW,
            wall_now=DEFAULT_NOW,
        )
        assert mutate.submit_calls == 0
        assert result.blocked is True
        orders = world.store.list_execution_orders(proposal_id=proposal.proposal_id)
        assert all(item.state is ExecutionState.NOT_SUBMITTED for item in orders)
        world.store.close()

    def test_stale_failure_is_not_unknown_and_does_not_duplicate(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "no-unknown")
        assert outcome.is_auto is True
        mutate = world.mutate()
        wall = DEFAULT_NOW + timedelta(seconds=16)
        first = _execute_at(
            world, proposal, prices, bindings, mutate, persist_now=DEFAULT_NOW, wall_now=wall
        )
        assert mutate.submit_calls == 0
        assert first.blocked is True
        orders = world.store.list_execution_orders(proposal_id=proposal.proposal_id)
        assert all(item.state is ExecutionState.NOT_SUBMITTED for item in orders)
        assert not world.store.list_audit(
            event_type=AuditEventType.ORDER_UNKNOWN, proposal_id=proposal.proposal_id
        )
        again = execute_reserved_proposal(
            world.store,
            world.read(),
            mutate,
            proposal,
            now=DEFAULT_NOW,
            prices=prices,
            price_bindings=bindings,
        )
        assert again.recovered is True
        assert mutate.submit_calls == 0
        recovered = recover_proposal(
            world.store, world.read(), proposal.proposal_id, now=DEFAULT_NOW
        )
        assert recovered.state is ExecutionState.NOT_SUBMITTED
        assert mutate.submit_calls == 0
        assert active_capacity(world.store, proposal.proposal_id) == 0
        world.store.close()
