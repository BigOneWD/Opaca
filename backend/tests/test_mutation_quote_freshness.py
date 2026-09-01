"""Final mutation-boundary quote re-fetch. Wall clock, not caller now."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from opaca.domain.models import Proposal
from opaca.execution.gateway import PaperMutatingGateway
from opaca.execution.service import ExecutionResult, execute_reserved_proposal, recover_proposal
from opaca.market.binding import BoundExecutionPrice, bind_buy, bind_single_leg_proposal
from opaca.market.errors import QuoteValidationError
from opaca.market.quote import IexLatestQuote
from opaca.market.source import FakeMarketData
from opaca.orchestration.reserve import OrchestrationResult, evaluate_and_reserve
from opaca.persistence.types import AuditEventType, ExecutionState
from opaca.reconciliation.service import reconcile

from tests.execution_helpers import PaperWorld, active_capacity, freeze_submit_clock, make_world
from tests.helpers import DEFAULT_NOW
from tests.market_helpers import iex_quote_matching_bound, universe_quotes


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
    market: FakeMarketData,
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
            market_data=market,
        )


def _fresh_final_quote(
    bound: BoundExecutionPrice,
    *,
    now: datetime,
    ask: Decimal | None = None,
    bid: Decimal | None = None,
    source_event_age: timedelta = timedelta(seconds=1),
    fetch_age: timedelta = timedelta(0),
) -> IexLatestQuote:
    ask_price = bound.quote.price if ask is None else ask
    bid_price = bound.quote.price if bid is None else bid
    if bid_price > ask_price:
        bid_price = ask_price
    return IexLatestQuote(
        symbol=bound.quote.symbol,
        bid_price=bid_price,
        ask_price=ask_price,
        bid_size=Decimal("1"),
        ask_size=Decimal("1"),
        source_timestamp=now - source_event_age,
        fetched_at=now - fetch_age,
        source=bound.quote.source,
    )


class TestFinalBoundaryFreshness:
    def test_quote_fresh_at_initial_evaluation(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        _proposal, _prices, _bindings, outcome = _reserve_bound(world, "fresh-eval")
        assert outcome.is_auto is True
        world.store.close()

    def test_final_mutation_boundary_re_fetches_quote(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "re-fetch")
        assert outcome.is_auto is True
        bound = bindings["SGOV"]
        market = FakeMarketData(quotes={"SGOV": iex_quote_matching_bound(bound)})
        mutate = world.mutate()
        result = _execute_at(
            world,
            proposal,
            prices,
            bindings,
            mutate,
            market,
            persist_now=DEFAULT_NOW,
            wall_now=DEFAULT_NOW,
        )
        assert result.submitted is True
        assert mutate.submit_calls == 1
        assert market.fetch_log == ["SGOV"]
        world.store.close()

    def test_final_buy_ask_at_or_below_approved_limit_may_submit(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "ask-ok")
        assert outcome.is_auto is True
        bound = bindings["SGOV"]
        market = FakeMarketData(
            quotes={"SGOV": _fresh_final_quote(bound, now=DEFAULT_NOW, ask=bound.limit_price)}
        )
        mutate = world.mutate()
        result = _execute_at(
            world,
            proposal,
            prices,
            bindings,
            mutate,
            market,
            persist_now=DEFAULT_NOW,
            wall_now=DEFAULT_NOW,
        )
        assert result.submitted is True
        assert mutate.submit_calls == 1
        assert mutate.orders[proposal.legs[0].client_order_id]["limit_price"] == format(
            bound.limit_price, "f"
        )
        world.store.close()

    def test_final_buy_ask_above_approved_limit_zero_submits(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "ask-high")
        assert outcome.is_auto is True
        bound = bindings["SGOV"]
        too_high = bound.limit_price + Decimal("0.01")
        market = FakeMarketData(
            quotes={"SGOV": _fresh_final_quote(bound, now=DEFAULT_NOW, ask=too_high, bid=too_high)}
        )
        mutate = world.mutate()
        result = _execute_at(
            world,
            proposal,
            prices,
            bindings,
            mutate,
            market,
            persist_now=DEFAULT_NOW,
            wall_now=DEFAULT_NOW,
        )
        assert mutate.submit_calls == 0
        assert result.submitted is False
        assert result.blocked is True
        assert result.block_reason is not None
        assert "exceeds approved BUY LIMIT" in result.block_reason
        orders = world.store.list_execution_orders(proposal_id=proposal.proposal_id)
        assert orders
        assert all(item.state is ExecutionState.NOT_SUBMITTED for item in orders)
        world.store.close()

    def test_final_fetch_failure_zero_submits(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "fetch-fail")
        assert outcome.is_auto is True
        market = FakeMarketData(quotes={}, unavailable=True)
        mutate = world.mutate()
        result = _execute_at(
            world,
            proposal,
            prices,
            bindings,
            mutate,
            market,
            persist_now=DEFAULT_NOW,
            wall_now=DEFAULT_NOW,
        )
        assert mutate.submit_calls == 0
        assert result.submitted is False
        assert result.blocked is True
        world.store.close()

    def test_final_malformed_quote_zero_submits(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "malformed")
        assert outcome.is_auto is True

        class _Malformed(FakeMarketData):
            def get_latest_quote(self, symbol: str) -> IexLatestQuote:
                self.fetch_log.append(symbol)
                raise QuoteValidationError("malformed quote payload; fail closed")

        market = _Malformed(quotes={})
        mutate = world.mutate()
        result = _execute_at(
            world,
            proposal,
            prices,
            bindings,
            mutate,
            market,
            persist_now=DEFAULT_NOW,
            wall_now=DEFAULT_NOW,
        )
        assert mutate.submit_calls == 0
        assert result.submitted is False
        assert market.fetch_log == ["SGOV"]
        world.store.close()

    def test_final_source_event_226s_may_submit(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "source-diagnostic")
        assert outcome.is_auto is True
        bound = bindings["SGOV"]
        market = FakeMarketData(
            quotes={
                "SGOV": _fresh_final_quote(
                    bound,
                    now=DEFAULT_NOW,
                    source_event_age=timedelta(seconds=226),
                )
            }
        )
        mutate = world.mutate()
        result = _execute_at(
            world,
            proposal,
            prices,
            bindings,
            mutate,
            market,
            persist_now=DEFAULT_NOW,
            wall_now=DEFAULT_NOW,
        )
        assert result.submitted is True
        assert mutate.submit_calls == 1
        diagnostics = world.store.list_audit(
            event_type=AuditEventType.EXECUTION_REVALIDATED, proposal_id=proposal.proposal_id
        )
        assert any(
            "source_event_age_seconds=" in event.detail and "(diagnostic)" in event.detail
            for event in diagnostics
        )
        world.store.close()

    def test_final_source_event_10_minutes_may_submit(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "source-10m")
        assert outcome.is_auto is True
        bound = bindings["SGOV"]
        market = FakeMarketData(
            quotes={
                "SGOV": _fresh_final_quote(
                    bound,
                    now=DEFAULT_NOW,
                    source_event_age=timedelta(minutes=10),
                )
            }
        )
        mutate = world.mutate()
        result = _execute_at(
            world,
            proposal,
            prices,
            bindings,
            mutate,
            market,
            persist_now=DEFAULT_NOW,
            wall_now=DEFAULT_NOW,
        )
        assert result.submitted is True
        assert mutate.submit_calls == 1
        world.store.close()

    def test_final_fetch_age_over_15s_zero_submits(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "fetch-stale")
        assert outcome.is_auto is True
        bound = bindings["SGOV"]
        market = FakeMarketData(
            quotes={
                "SGOV": _fresh_final_quote(
                    bound,
                    now=DEFAULT_NOW,
                    fetch_age=timedelta(seconds=15, microseconds=1),
                    source_event_age=timedelta(seconds=15, microseconds=1),
                )
            }
        )
        mutate = world.mutate()
        result = _execute_at(
            world,
            proposal,
            prices,
            bindings,
            mutate,
            market,
            persist_now=DEFAULT_NOW,
            wall_now=DEFAULT_NOW,
        )
        assert mutate.submit_calls == 0
        assert result.submitted is False
        assert result.blocked is True
        world.store.close()

    def test_kill_switch_flips_after_final_fetch_zero_submits(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "kill-after-fetch")
        assert outcome.is_auto is True
        bound = bindings["SGOV"]

        def flip() -> None:
            world.store.set_kill_switch(True, now=DEFAULT_NOW)

        market = FakeMarketData(
            quotes={"SGOV": iex_quote_matching_bound(bound)},
            after_fetch=flip,
        )
        mutate = world.mutate()
        result = _execute_at(
            world,
            proposal,
            prices,
            bindings,
            mutate,
            market,
            persist_now=DEFAULT_NOW,
            wall_now=DEFAULT_NOW,
        )
        assert market.fetch_log == ["SGOV"]
        assert mutate.submit_calls == 0
        assert result.submitted is False
        assert result.blocked is True
        orders = world.store.list_execution_orders(proposal_id=proposal.proposal_id)
        assert orders
        assert all(item.state is ExecutionState.NOT_SUBMITTED for item in orders)
        world.store.close()

    def test_missing_market_data_zero_submits(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "no-market")
        assert outcome.is_auto is True
        mutate = world.mutate()
        with freeze_submit_clock(DEFAULT_NOW):
            result = execute_reserved_proposal(
                world.store,
                world.read(),
                mutate,
                proposal,
                now=DEFAULT_NOW,
                prices=prices,
                price_bindings=bindings,
            )
        assert mutate.submit_calls == 0
        assert result.submitted is False
        assert result.blocked is True
        world.store.close()

    def test_stale_failure_is_not_unknown_and_does_not_duplicate(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "no-unknown")
        assert outcome.is_auto is True
        bound = bindings["SGOV"]
        market = FakeMarketData(
            quotes={
                "SGOV": _fresh_final_quote(
                    bound,
                    now=DEFAULT_NOW,
                    fetch_age=timedelta(seconds=16),
                    source_event_age=timedelta(seconds=16),
                )
            }
        )
        mutate = world.mutate()
        first = _execute_at(
            world,
            proposal,
            prices,
            bindings,
            mutate,
            market,
            persist_now=DEFAULT_NOW,
            wall_now=DEFAULT_NOW,
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
            market_data=market,
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

    def test_original_bound_age_does_not_replace_re_fetch(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal, prices, bindings, outcome = _reserve_bound(world, "old-bound-fresh-fetch")
        assert outcome.is_auto is True
        bound = bindings["SGOV"]
        wall = DEFAULT_NOW + timedelta(seconds=16)
        market = FakeMarketData(quotes={"SGOV": _fresh_final_quote(bound, now=wall)})
        mutate = world.mutate()
        result = _execute_at(
            world,
            proposal,
            prices,
            bindings,
            mutate,
            market,
            persist_now=DEFAULT_NOW,
            wall_now=wall,
        )
        assert result.submitted is True
        assert mutate.submit_calls == 1
        assert market.fetch_log == ["SGOV"]
        world.store.close()


class TestSellFinalBound:
    def test_final_sell_bid_below_approved_limit_zero_submits(self, tmp_path: Path) -> None:
        from opaca.market.binding import bind_sell

        world = make_world(tmp_path)
        quotes = universe_quotes(now=DEFAULT_NOW, age_seconds=1)
        bound = bind_sell(quotes["SGOV"], Decimal("1"))
        proposal, prices, bindings = bind_single_leg_proposal("sell-low", bound, quotes)
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
        assert outcome.is_auto is True
        low = bound.limit_price - Decimal("0.01")
        market = FakeMarketData(
            quotes={
                "SGOV": _fresh_final_quote(bound, now=DEFAULT_NOW, bid=low, ask=bound.limit_price)
            }
        )
        mutate = world.mutate()
        result = _execute_at(
            world,
            proposal,
            prices,
            bindings,
            mutate,
            market,
            persist_now=DEFAULT_NOW,
            wall_now=DEFAULT_NOW,
        )
        assert mutate.submit_calls == 0
        assert result.submitted is False
        assert result.block_reason is not None
        assert "approved SELL LIMIT" in result.block_reason
        world.store.close()
