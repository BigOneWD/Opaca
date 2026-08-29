"""No-leverage invariant (CHECK-06 / CHECK-11).

Required proof 4: $400k buying_power with $100k cash cannot authorize $400k
deployment. Broker cash fixture: Phase -1A evidence A1.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from opaca.authority.engine import decide_authority
from opaca.domain.models import AuthorityResult, CheckId, Side

from tests.helpers import (
    decide,
    evaluate,
    make_context,
    make_order,
    make_proposal,
    phase1_broker_cash,
)

FOUR_HUNDRED_K = Decimal("400000")


class TestBuyingPowerIsNotCorporateLiquidity:
    def test_phase1_broker_state_is_4x_leveraged(self) -> None:
        broker = phase1_broker_cash()
        assert broker.cash == Decimal("100000")
        assert broker.buying_power == Decimal("400000")
        assert broker.buying_power == broker.multiplier * broker.cash
        assert broker.non_marginable_buying_power == Decimal("100000")

    def test_buying_power_cannot_authorize_400k_deployment(self) -> None:
        """Required proof 4. The proposal fits within broker buying_power
        ($400k) but exceeds reconciled corporate cash; it must be rejected."""
        context = make_context()
        proposal = make_proposal(
            "prop-leverage",
            [make_order("prop-leverage", 0, "SGOV", Side.BUY, "3972", "100.70")],
        )
        assert proposal.total_buy_notional == Decimal("399980.40")
        assert proposal.total_buy_notional <= FOUR_HUNDRED_K
        decision = evaluate(proposal, context)
        assert not decision.passed
        for check_id in (CheckId.CHECK_01, CheckId.CHECK_06, CheckId.CHECK_11):
            assert not decision.result_for(check_id).passed

    def test_authority_rejects_the_leveraged_deployment(self) -> None:
        context = make_context()
        proposal = make_proposal(
            "prop-leverage-2",
            [make_order("prop-leverage-2", 0, "SGOV", Side.BUY, "3972", "100.70")],
        )
        decision = evaluate(proposal, context)
        authority = decide_authority(
            proposal,
            decision,
            context.authority_policy,
            context.autonomous_history,
            context.execution.now,
        )
        assert authority.result is AuthorityResult.REJECT

    def test_funding_ceiling_is_investable_cash_not_buying_power(self) -> None:
        from opaca.treasury.liquidity import compute_liquidity
        from opaca.treasury.scenario import seed_scenario

        seed = seed_scenario(Decimal("100000"), DATE_2026_09_01)
        projection = compute_liquidity(
            broker=phase1_broker_cash(),
            obligations=seed.obligations,
            settlement_events=(),
            operating_reserve=seed.operating_reserve,
            as_of=DATE_2026_09_01,
        )
        assert projection.funding_ceiling == Decimal("22000.00")
        assert projection.funding_ceiling < Decimal("400000")


class TestRoutineDeploymentInsideCashCeiling:
    def test_22k_split_buy_is_policy_valid_and_auto(self) -> None:
        """Positive control: exactly the investable surplus, split 70/30 so
        concentration stays within the 70% limit."""
        prices = {"SGOV": Decimal("100.00"), "BIL": Decimal("50.00"), "SHV": Decimal("110.00")}
        context = make_context(prices=prices)
        proposal = make_proposal(
            "prop-routine",
            [
                make_order("prop-routine", 0, "SGOV", Side.BUY, "154", "100.00"),
                make_order("prop-routine", 1, "BIL", Side.BUY, "132", "50.00"),
            ],
        )
        assert proposal.total_buy_notional == Decimal("22000.00")
        decision = evaluate(proposal, context)
        assert decision.passed, [f"{r.check_id}: {r.detail}" for r in decision.violations]
        assert decision.result_for(CheckId.CHECK_04).passed
        authority = decide(proposal, context)
        assert authority.result is AuthorityResult.AUTO

    def test_one_dollar_over_investable_is_rejected(self) -> None:
        prices = {"SGOV": Decimal("100.00"), "BIL": Decimal("50.00"), "SHV": Decimal("110.00")}
        context = make_context(prices=prices)
        proposal = make_proposal(
            "prop-over",
            [
                make_order("prop-over", 0, "SGOV", Side.BUY, "154", "100.00"),
                make_order("prop-over", 1, "BIL", Side.BUY, "132.02", "50.00"),
            ],
        )
        assert proposal.total_buy_notional == Decimal("22001.00")
        decision = evaluate(proposal, context)
        assert not decision.passed
        assert not decision.result_for(CheckId.CHECK_01).passed
        assert not decision.result_for(CheckId.CHECK_06).passed


DATE_2026_09_01 = date(2026, 9, 1)
