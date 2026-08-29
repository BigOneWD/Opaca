"""Concentration (CHECK-04): the INVESTMENT POOL BASE is the only
denominator (SPEC s9 CHECK-04, Amendment G; red-team RT-02).

    investment_pool_base =
        current market value of eligible investment holdings
        + current deployable investment cash

Deployable investment cash is the settlement-aware investable cash
(settled cash minus protected reserve and obligation-committed cash).
Total corporate cash is NOT part of the denominator. The pool base is
fixed at proposal evaluation time and stays the denominator for every
partial-fill subset, so unfilled investment cash keeps a partial fill
from showing a fake 100% concentration.
"""

from __future__ import annotations

from decimal import Decimal

from opaca.domain.models import CheckId, Position, Side
from opaca.policy.engine import CHECK_ORDER
from opaca.treasury.liquidity import investment_pool_base, project_portfolio

from tests.helpers import evaluate, make_context, make_order, make_proposal

PRICE = Decimal("100.00")
PRICES = {"SGOV": PRICE, "BIL": PRICE, "SHV": PRICE}

#: Default seed against 100,000 opening cash: payroll 24,000 + suppliers
#: 14,000 + reserve 40,000 leaves exactly 22,000 deployable.
DEFAULT_INVESTABLE = Decimal("22000.00")


def position(symbol: str, quantity: str) -> Position:
    qty = Decimal(quantity)
    return Position(symbol=symbol, quantity=qty, quantity_available=qty, market_value=qty * PRICE)


class TestInvestmentPoolBase:
    def test_pool_base_is_holdings_plus_deployable_cash_only(self) -> None:
        """The denominator is investment-pool capital, never total corporate
        cash: with the default seed, 78,000 of the 100,000 is protected and
        stays out of the pool."""
        context = make_context(prices=PRICES)
        pool = investment_pool_base(context.positions, context.prices, Decimal("22000.00"))
        assert pool == DEFAULT_INVESTABLE

    def test_negative_investable_cash_contributes_nothing(self) -> None:
        pool = investment_pool_base((position("SGOV", "100"),), PRICES, Decimal("-8000.00"))
        assert pool == Decimal("10000.00")


class TestHeroExample:
    """SPEC s19 Beat 3 firewall proof under Amendment G semantics."""

    def test_18480_sgov_proposal_is_84_percent_of_22000_pool_and_fails(self) -> None:
        context = make_context(prices=PRICES)
        proposal = make_proposal(
            "prop-hero", [make_order("prop-hero", 0, "SGOV", Side.BUY, "184.8", PRICE)]
        )
        assert proposal.total_buy_notional == Decimal("18480.00")
        decision = evaluate(proposal, context)
        result = decision.result_for(CheckId.CHECK_04)
        assert not result.passed
        assert "SGOV" in result.detail
        portfolio = project_portfolio(context.positions, proposal.legs, PRICES, DEFAULT_INVESTABLE)
        assert portfolio.investment_pool_base == DEFAULT_INVESTABLE
        assert portfolio.concentration_of("SGOV") == Decimal("18480.00") / Decimal("22000.00")

    def test_balanced_bootstrap_is_50_50_and_passes(self) -> None:
        context = make_context(prices=PRICES)
        proposal = make_proposal(
            "prop-bootstrap",
            [
                make_order("prop-bootstrap", 0, "SGOV", Side.BUY, "110", PRICE),
                make_order("prop-bootstrap", 1, "BIL", Side.BUY, "110", PRICE),
            ],
        )
        assert proposal.total_buy_notional == DEFAULT_INVESTABLE
        decision = evaluate(proposal, context)
        assert decision.result_for(CheckId.CHECK_04).passed
        portfolio = project_portfolio(context.positions, proposal.legs, PRICES, DEFAULT_INVESTABLE)
        assert portfolio.concentration_of("SGOV") == Decimal("0.5")
        assert portfolio.concentration_of("BIL") == Decimal("0.5")


class TestExistingPositionsIncluded:
    def test_existing_positions_are_included_in_concentration(self) -> None:
        """The violation comes from the EXISTING SGOV holding; the proposal
        itself is balanced and small."""
        context = make_context(
            prices=PRICES,
            positions=(position("SGOV", "900"), position("BIL", "100")),
        )
        proposal = make_proposal(
            "prop-conc",
            [
                make_order("prop-conc", 0, "BIL", Side.BUY, "50", PRICE),
                make_order("prop-conc", 1, "SHV", Side.BUY, "50", PRICE),
            ],
        )
        decision = evaluate(proposal, context)
        result = decision.result_for(CheckId.CHECK_04)
        assert not result.passed
        assert "SGOV" in result.detail
        pool = Decimal("100000.00") + DEFAULT_INVESTABLE
        portfolio = project_portfolio(context.positions, proposal.legs, PRICES, pool)
        assert portfolio.concentration_of("SGOV") == Decimal("90000.00") / pool

    def test_pre_existing_breach_blocks_even_balanced_buys(self) -> None:
        context = make_context(
            prices=PRICES,
            positions=(position("SGOV", "900"), position("BIL", "100")),
        )
        proposal = make_proposal(
            "prop-conc-2",
            [make_order("prop-conc-2", 0, "BIL", Side.BUY, "10", PRICE)],
        )
        decision = evaluate(proposal, context)
        assert not decision.result_for(CheckId.CHECK_04).passed


class TestCorrectDenominator:
    def test_denominator_is_the_pool_base_not_proposal_amounts(self) -> None:
        """A proposal-only view would call the single SHV leg 100% of the
        proposal and fail; the pool-base denominator yields 24.6% and the
        proposal passes."""
        context = make_context(
            prices=PRICES,
            positions=(position("BIL", "600"), position("SGOV", "400")),
        )
        proposal = make_proposal(
            "prop-denom",
            [make_order("prop-denom", 0, "SHV", Side.BUY, "300", PRICE)],
        )
        pool = Decimal("100000.00") + DEFAULT_INVESTABLE
        portfolio = project_portfolio(context.positions, proposal.legs, PRICES, pool)
        assert portfolio.investment_pool_base == pool
        assert portfolio.total_invested_value == Decimal("130000.00")
        assert portfolio.concentration_of("SHV") == Decimal("30000.00") / pool
        decision = evaluate(proposal, context)
        assert decision.result_for(CheckId.CHECK_04).passed

    def test_partial_fill_subset_keeps_the_fixed_pool_base(self) -> None:
        """Only SGOV fills: it stays 50% of the 22,000 pool because unfilled
        investment cash remains in the pool — no fake 100% concentration."""
        pool = DEFAULT_INVESTABLE
        sgov_leg = make_order("prop-pool", 0, "SGOV", Side.BUY, "110", PRICE)
        subset = project_portfolio((), (sgov_leg,), PRICES, pool)
        assert subset.concentration_of("SGOV") == Decimal("11000.00") / pool
        assert subset.concentration_of("SGOV") == Decimal("0.5")


class TestSellsReduceConcentration:
    def test_sell_is_allowed_to_reduce_concentration(self) -> None:
        context = make_context(
            prices=PRICES,
            positions=(position("SGOV", "900"), position("BIL", "100")),
        )
        assert not (
            evaluate(
                make_proposal("prop-hold", []),
                context,
            )
            .result_for(CheckId.CHECK_04)
            .passed
        )
        proposal = make_proposal(
            "prop-reduce",
            [make_order("prop-reduce", 0, "SGOV", Side.SELL, "200", PRICE)],
        )
        decision = evaluate(proposal, context)
        assert decision.result_for(CheckId.CHECK_04).passed

    def test_full_liquidation_passes_without_a_vacuous_branch(self) -> None:
        context = make_context(
            prices=PRICES,
            positions=(position("SGOV", "100"), position("BIL", "100")),
        )
        proposal = make_proposal(
            "prop-flatten",
            [
                make_order("prop-flatten", 0, "SGOV", Side.SELL, "100", PRICE),
                make_order("prop-flatten", 1, "BIL", Side.SELL, "100", PRICE),
            ],
        )
        decision = evaluate(proposal, context)
        result = decision.result_for(CheckId.CHECK_04)
        assert result.passed
        assert "vacuous" not in result.detail


class TestBoundaries:
    def test_no_invested_value_is_vacuously_compliant(self) -> None:
        context = make_context(obligations=(), operating_reserve=Decimal("0"))
        proposal = make_proposal("prop-empty", [])
        decision = evaluate(proposal, context)
        assert decision.result_for(CheckId.CHECK_04).passed

    def test_exactly_at_limit_passes(self) -> None:
        context = make_context(prices=PRICES)
        proposal = make_proposal(
            "prop-at-limit",
            [
                make_order("prop-at-limit", 0, "SGOV", Side.BUY, "154", PRICE),
                make_order("prop-at-limit", 1, "BIL", Side.BUY, "66", PRICE),
            ],
        )
        decision = evaluate(proposal, context)
        assert decision.result_for(CheckId.CHECK_04).passed

    def test_zero_pool_with_projected_holdings_fails_closed(self) -> None:
        """Zero deployable cash and no holdings: a projected holding cannot
        be funded by the pool, so concentration fails closed."""
        from opaca.domain.models import BrokerCashState

        from tests.helpers import DEFAULT_NOW

        zero_broker = BrokerCashState(
            cash=Decimal("38000.00"),
            buying_power=Decimal("0"),
            non_marginable_buying_power=Decimal("38000.00"),
            multiplier=Decimal("1"),
            as_of=DEFAULT_NOW,
        )
        context = make_context(prices=PRICES, broker=zero_broker)
        proposal = make_proposal(
            "prop-zeropool",
            [make_order("prop-zeropool", 0, "SGOV", Side.BUY, "1", PRICE)],
        )
        result = evaluate(proposal, context).result_for(CheckId.CHECK_04)
        assert not result.passed


class TestDecisionShape:
    def test_results_follow_check_order(self) -> None:
        context = make_context()
        proposal = make_proposal(
            "prop-order",
            [make_order("prop-order", 0, "SGOV", Side.BUY, "1", "100.00")],
        )
        decision = evaluate(proposal, context)
        assert [r.check_id for r in decision.results] == list(CHECK_ORDER)
