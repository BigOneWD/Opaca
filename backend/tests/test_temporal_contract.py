"""Temporal contract for quote fetches, reservation, and live-paper authority."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from opaca.domain.models import Side
from opaca.market.binding import BoundExecutionPrice, bind_buy, bind_single_leg_proposal
from opaca.market.errors import FutureQuoteError, StaleQuoteError
from opaca.market.quote import (
    DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS,
    CanonicalMarketPrice,
    IexLatestQuote,
    executable_canonical_price,
)
from opaca.market.source import FakeMarketData, required_canonical_prices
from opaca.orchestration.reserve import OrchestrationResult, evaluate_and_reserve
from opaca.persistence.types import ReconciliationStatus
from opaca.reconciliation.service import reconcile

from tests.execution_helpers import make_world
from tests.helpers import DEFAULT_NOW


def _quote(*, fetched_at_offset: timedelta) -> IexLatestQuote:
    fetched_at = DEFAULT_NOW + fetched_at_offset
    return IexLatestQuote(
        symbol="SGOV",
        bid_price=Decimal("100.69"),
        ask_price=Decimal("100.70"),
        bid_size=Decimal("1"),
        ask_size=Decimal("1"),
        source_timestamp=fetched_at - timedelta(seconds=1),
        fetched_at=fetched_at,
        source="alpaca.stock.latest_quote.iex",
    )


def _required_canonical(*, fetched_at_offset: timedelta) -> dict[str, CanonicalMarketPrice]:
    return required_canonical_prices(
        FakeMarketData(quotes={"SGOV": _quote(fetched_at_offset=fetched_at_offset)}),
        proposal_symbols=("SGOV",),
        side_by_symbol={"SGOV": Side.BUY},
        positions=(),
        now=DEFAULT_NOW,
    )


def _reserved_buy(
    tmp_path: Path,
    *,
    fetched_at_offset: timedelta,
    decision_now_offset: timedelta,
) -> tuple[OrchestrationResult, Mapping[str, Decimal], Mapping[str, BoundExecutionPrice]]:
    world = make_world(tmp_path)
    canonical = {
        "SGOV": executable_canonical_price(_quote(fetched_at_offset=fetched_at_offset), Side.BUY)
    }
    bound = bind_buy(canonical["SGOV"], Decimal("1"), tolerance=Decimal("0"))
    proposal, prices, bindings = bind_single_leg_proposal("temporal-buy", bound, canonical)
    recon = reconcile(world.store, world.read(), now=DEFAULT_NOW)
    assert recon.status is ReconciliationStatus.RECONCILED
    assert recon.snapshot is not None
    try:
        outcome = evaluate_and_reserve(
            world.store,
            proposal,
            now=DEFAULT_NOW + decision_now_offset,
            prices=prices,
            expected_snapshot_version=recon.snapshot.version,
            price_bindings=bindings,
        )
        return outcome, prices, bindings
    finally:
        world.store.close()


class TestRequiredCanonicalForwardAllowance:
    def test_fetch_two_point_three_five_seconds_after_t0_is_valid(self) -> None:
        canonical = _required_canonical(fetched_at_offset=timedelta(seconds=2.35))
        assert canonical["SGOV"].fetched_at == DEFAULT_NOW + timedelta(seconds=2.35)

    def test_fetch_exactly_fifteen_seconds_after_t0_is_valid(self) -> None:
        canonical = _required_canonical(
            fetched_at_offset=timedelta(seconds=DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS)
        )
        assert canonical["SGOV"].fetched_at == DEFAULT_NOW + timedelta(seconds=15)

    def test_fetch_more_than_fifteen_seconds_after_t0_is_blocked(self) -> None:
        with pytest.raises(FutureQuoteError, match="exceeds max fetch age"):
            _required_canonical(
                fetched_at_offset=timedelta(
                    seconds=DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS, microseconds=1
                )
            )

    def test_fetch_stale_by_more_than_fifteen_seconds_is_blocked(self) -> None:
        with pytest.raises(StaleQuoteError, match="fetch age"):
            required_canonical_prices(
                FakeMarketData(
                    quotes={
                        "SGOV": _quote(
                            fetched_at_offset=timedelta(
                                seconds=-(DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS),
                                microseconds=-1,
                            )
                        )
                    }
                ),
                proposal_symbols=("SGOV",),
                side_by_symbol={"SGOV": Side.BUY},
                positions=(),
                now=DEFAULT_NOW,
            )


class TestReservationDecisionClock:
    @pytest.mark.parametrize(
        "future_offset",
        (
            timedelta(microseconds=1),
            timedelta(hours=1),
            timedelta(days=30),
        ),
    )
    def test_quote_future_to_decision_clock_is_blocked(
        self,
        tmp_path: Path,
        future_offset: timedelta,
    ) -> None:
        decision_offset = timedelta(seconds=3)
        outcome, _prices, _bindings = _reserved_buy(
            tmp_path,
            fetched_at_offset=decision_offset + future_offset,
            decision_now_offset=decision_offset,
        )
        assert outcome.blocked is True
        assert outcome.reserved is False
        assert outcome.block_reason is not None
        assert "future" in outcome.block_reason


class TestOfflineLiveHarness:
    def test_post_fetch_decision_clock_reaches_auto_evaluation(self, tmp_path: Path) -> None:
        t0 = DEFAULT_NOW
        t1 = t0 + timedelta(seconds=2.35)
        t2 = t0 + timedelta(seconds=3)
        world = make_world(tmp_path, now=t0)
        canonical = required_canonical_prices(
            FakeMarketData(quotes={"SGOV": _quote(fetched_at_offset=t1 - t0)}),
            proposal_symbols=("SGOV",),
            side_by_symbol={"SGOV": Side.BUY},
            positions=(),
            now=t0,
        )
        bound = bind_buy(canonical["SGOV"], Decimal("1"), tolerance=Decimal("0"))
        proposal, prices, bindings = bind_single_leg_proposal("delayed-buy", bound, canonical)
        recon = reconcile(world.store, world.read(), now=t0)
        assert recon.status is ReconciliationStatus.RECONCILED
        assert recon.snapshot is not None
        outcome = evaluate_and_reserve(
            world.store,
            proposal,
            now=t2,
            prices=prices,
            expected_snapshot_version=recon.snapshot.version,
            price_bindings=bindings,
        )
        assert outcome.is_auto is True
        world.store.close()
