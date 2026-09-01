"""Bounded BUY LIMIT economics. Max cash uses LIMIT, not the lower print."""

from __future__ import annotations

from decimal import ROUND_UP, Decimal
from pathlib import Path

from opaca.domain.money import CENT
from opaca.execution.service import execute_reserved_proposal
from opaca.market.binding import bind_buy, bind_sell, bind_single_leg_proposal
from opaca.market.limit import (
    DEFAULT_BUY_LIMIT_TOLERANCE,
    buy_limit_price,
    max_buy_cash_obligation,
    sell_modeled_price,
)
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.types import ReservationKind
from opaca.reconciliation.service import reconcile

from tests.execution_helpers import freeze_submit_clock, make_world
from tests.helpers import DEFAULT_NOW
from tests.market_helpers import canonical_quote, market_data_from_bindings, universe_quotes


class TestLimitCalculation:
    def test_bounded_limit_price_calculation(self) -> None:
        canonical = Decimal("100.00")
        limit = buy_limit_price(canonical, tolerance=Decimal("0.01"))
        assert limit == Decimal("101.00")
        assert limit > canonical
        default_limit = buy_limit_price(Decimal("100.69"))
        expected = Decimal("100.69") * (Decimal("1") + DEFAULT_BUY_LIMIT_TOLERANCE)
        assert default_limit == expected.quantize(CENT, rounding=ROUND_UP)

    def test_maximum_buy_cash_uses_limit_not_lower_reference(self) -> None:
        quote = canonical_quote(price=Decimal("100.00"))
        bound = bind_buy(quote, Decimal("1"), tolerance=Decimal("0.01"))
        assert bound.valuation_price == Decimal("100.00")
        assert bound.limit_price == Decimal("101.00")
        assert bound.reference_price == bound.limit_price
        assert bound.max_cash_obligation == Decimal("101.00")
        assert bound.max_cash_obligation == max_buy_cash_obligation(Decimal("1"), bound.limit_price)
        assert bound.max_cash_obligation != max_buy_cash_obligation(
            Decimal("1"), bound.valuation_price
        )

    def test_sell_is_not_marked_up(self) -> None:
        quote = canonical_quote(price=Decimal("100.00"))
        bound = bind_sell(quote, Decimal("1"))
        assert bound.reference_price == Decimal("100.00")
        assert bound.limit_price == Decimal("100.00")
        assert sell_modeled_price(quote) == quote.price
        assert bound.max_cash_obligation is None


class TestSubmittedEconomics:
    def test_treasuryguard_and_submitted_order_economics_agree(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        quotes = universe_quotes(sgov=Decimal("100.00"))
        bound = bind_buy(quotes["SGOV"], Decimal("1"), tolerance=Decimal("0.01"))
        proposal, prices, bindings = bind_single_leg_proposal("econ-1", bound, quotes)
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
        assert proposal.total_buy_notional == bound.max_cash_obligation
        cash = [
            item.amount
            for item in world.store.active_reservations()
            if item.proposal_id == "econ-1" and item.kind is ReservationKind.CASH_DEPLOYMENT
        ]
        assert cash == [Decimal("101.00")]
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
                market_data=market_data_from_bindings(bindings),
            )
        assert result.blocked is False
        assert result.submitted is True
        cid = proposal.legs[0].client_order_id
        assert mutate.orders[cid]["limit_price"] == "101.00"
        assert mutate.orders[cid]["order_type"] == "limit"
        assert mutate.submit_calls == 1
        world.store.close()
