"""Concentration (CHECK-04): the INVESTMENT POOL BASE is the only
denominator (SPEC s9 CHECK-04, Amendment G; red-team RT-02, NEW-01,
NEW-03).

    investment_pool_base =
        current market value of ELIGIBLE investment holdings
        + current deployable investment cash

Eligible holdings are the holdings in InvestmentPolicy.permitted_symbols:
a non-whitelisted / manual / legacy holding buys no concentration headroom
and is not itself an offender (NEW-01). Deployable investment cash is the
settlement-aware investable cash (settled cash minus protected reserve and
obligation-committed cash). Total corporate cash is NOT part of the
denominator. The pool base is fixed at proposal evaluation time and stays
the denominator for every partial-fill subset, so unfilled investment cash
keeps a partial fill from showing a fake 100% concentration.

From a pre-existing breach, CHECK-04 additionally permits monotonic
de-risking (NEW-03): a proposal may pass with the projection still above
the limit only if every pre-existing offending symbol is strictly improved
and no previously compliant symbol becomes a new offender. The rule is
improvement-based, not side-based: there is no blanket sell exemption.
"""

from __future__ import annotations

from decimal import Decimal

from opaca.domain.models import CheckId, Position, Side
from opaca.policy.engine import CHECK_ORDER, PolicyContext
from opaca.treasury.liquidity import investment_pool_base, project_portfolio

from tests.helpers import evaluate, make_context, make_order, make_proposal

PRICE = Decimal("100.00")
PRICES = {"SGOV": PRICE, "BIL": PRICE, "SHV": PRICE}
PRICES_WITH_XYZ = {**PRICES, "XYZ": PRICE}
PERMITTED = frozenset({"SGOV", "BIL", "SHV"})

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


class TestEligibleHoldingsOnly:
    """NEW-01: non-whitelisted holdings buy no concentration headroom and
    are not CHECK-04 offenders."""

    def test_ineligible_holding_does_not_inflate_the_pool(self) -> None:
        def max_passing_sgov_notional(xyz_value: str) -> Decimal | None:
            positions: tuple[Position, ...] = ()
            if Decimal(xyz_value) > 0:
                qty = Decimal(xyz_value) / PRICE
                positions = (
                    Position(
                        symbol="XYZ",
                        quantity=qty,
                        quantity_available=Decimal("0"),
                        market_value=Decimal("0"),
                    ),
                )
            context = make_context(
                cash="100000",
                prices=PRICES_WITH_XYZ,
                positions=positions,
                obligations=(),
                operating_reserve=Decimal("0"),
            )
            best: Decimal | None = None
            candidates = (
                Decimal("70000"),
                Decimal("80000"),
                Decimal("100000"),
                Decimal("140000"),
            )
            for notional in candidates:
                proposal = make_proposal(
                    "n1",
                    [make_order("n1", 0, "SGOV", Side.BUY, str(notional / PRICE), PRICE)],
                )
                if evaluate(proposal, context).result_for(CheckId.CHECK_04).passed:
                    best = notional
            return best

        assert max_passing_sgov_notional("0") == Decimal("70000")
        assert max_passing_sgov_notional("100000") == Decimal("70000")

    def test_permitted_existing_holding_is_included_in_the_pool(self) -> None:
        pool = investment_pool_base(
            (position("SGOV", "500"),),
            PRICES,
            Decimal("100000.00"),
            PERMITTED,
        )
        assert pool == Decimal("150000.00")

    def test_mixed_permitted_and_non_permitted_holdings(self) -> None:
        positions = (position("SGOV", "300"), position("XYZ", "500"))
        pool = investment_pool_base(positions, PRICES_WITH_XYZ, Decimal("100000.00"), PERMITTED)
        assert pool == Decimal("130000.00")
        context = make_context(
            cash="100000",
            prices=PRICES_WITH_XYZ,
            positions=positions,
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        at_limit = make_proposal(
            "n1-mix", [make_order("n1-mix", 0, "SGOV", Side.BUY, "610", PRICE)]
        )
        over = make_proposal(
            "n1-mix-over", [make_order("n1-mix-over", 0, "SGOV", Side.BUY, "620", PRICE)]
        )
        assert evaluate(at_limit, context).result_for(CheckId.CHECK_04).passed
        assert not evaluate(over, context).result_for(CheckId.CHECK_04).passed

    def test_missing_price_for_non_permitted_holding_does_not_contaminate_pool(self) -> None:
        pool = investment_pool_base(
            (position("XYZ", "500"), position("SGOV", "100")),
            PRICES,
            Decimal("100000.00"),
            PERMITTED,
        )
        assert pool == Decimal("110000.00")

    def test_missing_price_for_permitted_held_symbol_fails_closed(self) -> None:
        context = make_context(
            prices={"BIL": PRICE},
            positions=(position("SGOV", "10"),),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal("n1-miss", [make_order("n1-miss", 0, "BIL", Side.BUY, "1", PRICE)])
        result = evaluate(proposal, context).result_for(CheckId.CHECK_04)
        assert not result.passed
        assert "fail closed" in result.detail

    def test_trading_a_non_permitted_symbol_is_still_check_03(self) -> None:
        context = make_context(
            cash="100000",
            prices=PRICES_WITH_XYZ,
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal("n1-xyz", [make_order("n1-xyz", 0, "XYZ", Side.BUY, "1", PRICE)])
        assert not evaluate(proposal, context).result_for(CheckId.CHECK_03).passed


class TestMonotonicDeRisking:
    """NEW-03: a pre-existing breach may remain above the limit only while
    every offender strictly improves and no new offender appears."""

    def _overconcentrated_context(self) -> PolicyContext:
        return make_context(
            cash="5000",
            prices=PRICES,
            positions=(position("SGOV", "950"),),
            obligations=(),
            operating_reserve=Decimal("0"),
        )

    def test_empty_proposal_from_95_percent_fails(self) -> None:
        context = self._overconcentrated_context()
        assert not evaluate(make_proposal("n3z", []), context).result_for(CheckId.CHECK_04).passed

    def test_95_to_85_passes_as_monotonic_de_risking(self) -> None:
        context = self._overconcentrated_context()
        proposal = make_proposal("n3a", [make_order("n3a", 0, "SGOV", Side.SELL, "100", PRICE)])
        result = evaluate(proposal, context).result_for(CheckId.CHECK_04)
        assert result.passed
        assert "monotonic de-risking" in result.detail

    def test_95_to_95_fails(self) -> None:
        context = self._overconcentrated_context()
        proposal = make_proposal("n3same", [make_order("n3same", 0, "BIL", Side.BUY, "1", PRICE)])
        assert not evaluate(proposal, context).result_for(CheckId.CHECK_04).passed

    def test_95_to_96_fails(self) -> None:
        context = self._overconcentrated_context()
        proposal = make_proposal(
            "n3worse", [make_order("n3worse", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        assert not evaluate(proposal, context).result_for(CheckId.CHECK_04).passed

    def test_improving_sgov_while_creating_a_new_offender_fails(self) -> None:
        context = self._overconcentrated_context()
        proposal = make_proposal(
            "n3new",
            [
                make_order("n3new", 0, "SGOV", Side.SELL, "100", PRICE),
                make_order("n3new", 1, "BIL", Side.BUY, "750", PRICE),
            ],
        )
        result = evaluate(proposal, context).result_for(CheckId.CHECK_04)
        assert not result.passed
        assert "new offender" in result.detail

    def test_full_cure_passes(self) -> None:
        context = self._overconcentrated_context()
        proposal = make_proposal(
            "n3cure", [make_order("n3cure", 0, "SGOV", Side.SELL, "300", PRICE)]
        )
        result = evaluate(proposal, context).result_for(CheckId.CHECK_04)
        assert result.passed
        assert "monotonic de-risking" not in result.detail
