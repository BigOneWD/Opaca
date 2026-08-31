"""PolicyContext.prices and leg.reference_price must share one canonical quote."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from opaca.domain.models import Side
from opaca.execution.service import execute_reserved_proposal
from opaca.market.binding import (
    bind_buy,
    bind_single_leg_proposal,
    price_binding_failure,
    require_price_binding,
)
from opaca.market.errors import PriceBindingError
from opaca.market.quote import CanonicalMarketPrice
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.store import SQLiteStore
from opaca.persistence.types import ReservationKind
from opaca.reconciliation.service import reconcile

from tests.execution_helpers import freeze_submit_clock, make_world
from tests.helpers import DEFAULT_NOW, DEFAULT_PRICES, make_order, make_proposal
from tests.market_helpers import canonical_quote, universe_quotes


class TestMismatchRejected:
    def test_policy_and_reference_mismatch_rejected(self) -> None:
        proposal = make_proposal(
            "mismatch",
            [make_order("mismatch", 0, "SGOV", Side.BUY, "1", Decimal("0.01"))],
        )
        prices = {
            "SGOV": Decimal("100.00"),
            "BIL": DEFAULT_PRICES["BIL"],
            "SHV": DEFAULT_PRICES["SHV"],
        }
        reason = price_binding_failure(proposal, prices)
        assert reason is not None
        assert "0.01" in reason
        assert "100.00" in reason
        with pytest.raises(PriceBindingError, match="price binding mismatch"):
            require_price_binding(proposal, prices)

    def test_zero_point_zero_one_reference_price_attack_blocked(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = make_proposal(
            "exploit-001",
            [make_order("exploit-001", 0, "SGOV", Side.BUY, "1", Decimal("0.01"))],
        )
        prices = {
            "SGOV": Decimal("100.00"),
            "BIL": DEFAULT_PRICES["BIL"],
            "SHV": DEFAULT_PRICES["SHV"],
        }
        recon = reconcile(world.store, world.read(), now=DEFAULT_NOW)
        assert recon.snapshot is not None
        outcome = evaluate_and_reserve(
            world.store,
            proposal,
            now=DEFAULT_NOW,
            prices=prices,
            expected_snapshot_version=recon.snapshot.version,
        )
        assert outcome.blocked is True
        assert outcome.is_auto is False
        assert outcome.authority_result is None
        assert outcome.block_reason is not None
        assert "price binding mismatch" in outcome.block_reason
        mutate = world.mutate()
        result = execute_reserved_proposal(
            world.store,
            world.read(),
            mutate,
            proposal,
            now=DEFAULT_NOW,
            prices=prices,
        )
        assert result.blocked is True
        assert result.submitted is False
        assert mutate.submit_calls == 0
        world.store.close()


class TestBoundConstruction:
    def test_live_paper_constructor_binds_one_quote(self) -> None:
        quotes = universe_quotes(sgov=Decimal("100.00"))
        bound = bind_buy(quotes["SGOV"], Decimal("1"))
        proposal, prices, bindings = bind_single_leg_proposal("bound-1", bound, quotes)
        require_price_binding(proposal, prices, bindings=bindings)
        assert prices["SGOV"] == Decimal("100.00")
        assert proposal.legs[0].reference_price == bound.limit_price
        assert bound.limit_price > bound.valuation_price
        assert price_binding_failure(proposal, prices) is not None

    def test_injected_cheap_reference_cannot_use_bindings_from_real_quote(self) -> None:
        quotes = universe_quotes(sgov=Decimal("100.00"))
        bound = bind_buy(quotes["SGOV"], Decimal("1"))
        cheap = make_proposal(
            "cheap",
            [make_order("cheap", 0, "SGOV", Side.BUY, "1", Decimal("0.01"))],
        )
        prices = {symbol: quote.price for symbol, quote in quotes.items()}
        reason = price_binding_failure(cheap, prices, bindings={"SGOV": bound})
        assert reason is not None
        assert cheap.legs[0].reference_price == Decimal("0.01")


class TestRestart:
    def test_canonical_price_binding_survives_restart(self, tmp_path: Path) -> None:
        quotes = universe_quotes(sgov=Decimal("100.00"))
        quote = quotes["SGOV"]
        restored = CanonicalMarketPrice(
            symbol=quote.symbol,
            price=quote.price,
            source_timestamp=quote.source_timestamp,
            fetched_at=quote.fetched_at,
            source=quote.source,
        )
        assert restored == quote
        bound = bind_buy(quote, Decimal("1"))
        rebound = bind_buy(restored, Decimal("1"))
        assert rebound == bound
        world = make_world(tmp_path)
        proposal, prices, bindings = bind_single_leg_proposal("restart-1", bound, quotes)
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
        path = world.store.path
        world.store.close()
        reopened = SQLiteStore(path)
        loaded = reopened.load_proposal("restart-1")
        assert loaded is not None
        assert loaded.legs[0].reference_price == bound.reference_price
        assert loaded.legs[0].reference_price == rebound.reference_price
        require_price_binding(loaded, prices, bindings=bindings)
        cash = [
            item.amount
            for item in reopened.active_reservations()
            if item.proposal_id == "restart-1" and item.kind is ReservationKind.CASH_DEPLOYMENT
        ]
        assert cash == [bound.max_cash_obligation]
        reopened.close()


class TestStaleBindingCannotAuthorize:
    def test_stale_bound_quote_cannot_authorize_later(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        quotes = universe_quotes(age_seconds=1)
        bound = bind_buy(quotes["SGOV"], Decimal("1"))
        proposal, prices, bindings = bind_single_leg_proposal("stale-auth", bound, quotes)
        recon = reconcile(world.store, world.read(), now=DEFAULT_NOW)
        assert recon.snapshot is not None
        first = evaluate_and_reserve(
            world.store,
            proposal,
            now=DEFAULT_NOW,
            prices=prices,
            expected_snapshot_version=recon.snapshot.version,
            price_bindings=bindings,
        )
        assert first.is_auto is True
        later = DEFAULT_NOW + timedelta(seconds=20)
        mutate = world.mutate()
        result = execute_reserved_proposal(
            world.store,
            world.read(),
            mutate,
            proposal,
            now=later,
            prices=prices,
            price_bindings=bindings,
        )
        assert result.blocked is True
        assert result.submitted is False
        assert mutate.submit_calls == 0
        world.store.close()


class TestMandatoryBindings:
    def test_no_bindings_with_real_paper_gateway_blocked(self, tmp_path: Path) -> None:
        from opaca.broker.gateway import PAPER_ENDPOINT
        from opaca.broker.paper_execution import AlpacaPaperExecutionGateway

        class PaperClient:
            _base_url = PAPER_ENDPOINT
            _paper = True
            submits = 0

            def submit_order(self, *args: object, **kwargs: object) -> str:
                PaperClient.submits += 1
                raise AssertionError("must not submit")

            def cancel_order_by_id(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("must not cancel")

        world = make_world(tmp_path)
        proposal = make_proposal(
            "no-bind-real",
            [make_order("no-bind-real", 0, "SGOV", Side.BUY, "1", DEFAULT_PRICES["SGOV"])],
        )
        recon = reconcile(world.store, world.read(), now=DEFAULT_NOW)
        assert recon.snapshot is not None
        evaluate_and_reserve(
            world.store,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=recon.snapshot.version,
        )
        gateway = AlpacaPaperExecutionGateway(PaperClient())
        result = execute_reserved_proposal(
            world.store,
            world.read(),
            gateway,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
        )
        assert result.blocked is True
        assert result.submitted is False
        assert PaperClient.submits == 0
        world.store.close()

    def test_empty_bindings_blocked(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = make_proposal(
            "empty-bind",
            [make_order("empty-bind", 0, "SGOV", Side.BUY, "1", DEFAULT_PRICES["SGOV"])],
        )
        recon = reconcile(world.store, world.read(), now=DEFAULT_NOW)
        assert recon.snapshot is not None
        evaluate_and_reserve(
            world.store,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=recon.snapshot.version,
        )
        mutate = world.mutate()
        result = execute_reserved_proposal(
            world.store,
            world.read(),
            mutate,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            price_bindings={},
        )
        assert result.blocked is True
        assert mutate.submit_calls == 0
        world.store.close()

    def test_missing_one_leg_in_multi_leg_blocked(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = make_proposal(
            "miss-leg",
            [
                make_order("miss-leg", 0, "SGOV", Side.BUY, "1", DEFAULT_PRICES["SGOV"]),
                make_order("miss-leg", 1, "BIL", Side.BUY, "1", DEFAULT_PRICES["BIL"]),
            ],
        )
        recon = reconcile(world.store, world.read(), now=DEFAULT_NOW)
        assert recon.snapshot is not None
        evaluate_and_reserve(
            world.store,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=recon.snapshot.version,
        )
        sgov = bind_buy(
            canonical_quote("SGOV", DEFAULT_PRICES["SGOV"]), Decimal("1"), tolerance=Decimal("0")
        )
        mutate = world.mutate()
        result = execute_reserved_proposal(
            world.store,
            world.read(),
            mutate,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            price_bindings={"SGOV": sgov},
        )
        assert result.blocked is True
        assert mutate.submit_calls == 0
        world.store.close()

    def test_wrong_symbol_blocked(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = make_proposal(
            "wrong-sym",
            [make_order("wrong-sym", 0, "SGOV", Side.BUY, "1", DEFAULT_PRICES["SGOV"])],
        )
        recon = reconcile(world.store, world.read(), now=DEFAULT_NOW)
        assert recon.snapshot is not None
        evaluate_and_reserve(
            world.store,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=recon.snapshot.version,
        )
        bil = bind_buy(
            canonical_quote("BIL", DEFAULT_PRICES["BIL"]), Decimal("1"), tolerance=Decimal("0")
        )
        mutate = world.mutate()
        result = execute_reserved_proposal(
            world.store,
            world.read(),
            mutate,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            price_bindings={"SGOV": bil},
        )
        assert result.blocked is True
        assert mutate.submit_calls == 0
        world.store.close()

    def test_wrong_quantity_blocked(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        proposal = make_proposal(
            "wrong-qty",
            [make_order("wrong-qty", 0, "SGOV", Side.BUY, "1", DEFAULT_PRICES["SGOV"])],
        )
        recon = reconcile(world.store, world.read(), now=DEFAULT_NOW)
        assert recon.snapshot is not None
        evaluate_and_reserve(
            world.store,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=recon.snapshot.version,
        )
        bound = bind_buy(
            canonical_quote("SGOV", DEFAULT_PRICES["SGOV"]), Decimal("5"), tolerance=Decimal("0")
        )
        mutate = world.mutate()
        result = execute_reserved_proposal(
            world.store,
            world.read(),
            mutate,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            price_bindings={"SGOV": bound},
        )
        assert result.blocked is True
        assert mutate.submit_calls == 0
        world.store.close()

    def test_matched_invented_pair_blocked(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        invented = Decimal("0.01")
        proposal = make_proposal(
            "invented",
            [make_order("invented", 0, "SGOV", Side.SELL, "100", invented)],
        )
        prices = {"SGOV": invented, "BIL": invented, "SHV": invented}
        recon = reconcile(world.store, world.read(), now=DEFAULT_NOW)
        assert recon.snapshot is not None
        outcome = evaluate_and_reserve(
            world.store,
            proposal,
            now=DEFAULT_NOW,
            prices=prices,
            expected_snapshot_version=recon.snapshot.version,
        )
        assert outcome.is_auto is True
        mutate = world.mutate()
        result = execute_reserved_proposal(
            world.store,
            world.read(),
            mutate,
            proposal,
            now=DEFAULT_NOW,
            prices=prices,
        )
        assert result.blocked is True
        assert result.submitted is False
        assert mutate.submit_calls == 0
        world.store.close()

    def test_caller_changes_price_after_reservation_blocked(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        quotes = universe_quotes(sgov=Decimal("100.00"))
        bound = bind_buy(quotes["SGOV"], Decimal("1"))
        proposal, prices, bindings = bind_single_leg_proposal("changed", bound, quotes)
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
        cheap = dict(prices)
        cheap["SGOV"] = Decimal("0.01")
        mutate = world.mutate()
        result = execute_reserved_proposal(
            world.store,
            world.read(),
            mutate,
            proposal,
            now=DEFAULT_NOW,
            prices=cheap,
            price_bindings=bindings,
        )
        assert result.blocked is True
        assert mutate.submit_calls == 0
        world.store.close()

    def test_valid_canonical_binding_proceeds(self, tmp_path: Path) -> None:
        world = make_world(tmp_path)
        quotes = universe_quotes()
        bound = bind_buy(quotes["SGOV"], Decimal("1"), tolerance=Decimal("0"))
        proposal, prices, bindings = bind_single_leg_proposal("valid-bind", bound, quotes)
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
        assert result.blocked is False
        assert result.submitted is True
        assert mutate.submit_calls == 1
        world.store.close()
