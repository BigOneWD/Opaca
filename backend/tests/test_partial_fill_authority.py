"""RT-06: partial-fill safety is wired into the authority decision BEFORE
AUTO. An unsafe partial-fill assessment is a hard safety failure — REJECT —
that human approval cannot silently override; a proposal whose base
evaluation passes must still never reach AUTO while its assessment is
unsafe. The base TreasuryGuard evaluation remains separately callable by
the subset evaluator (no recursion)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from opaca.authority.engine import apply_human_approval, decide_authority
from opaca.domain.models import (
    AuthorityPolicy,
    AuthorityResult,
    BrokerCashState,
    CheckId,
    Obligation,
    PartialFillAssessment,
    PolicyDecision,
    Position,
    Proposal,
    Side,
)
from opaca.policy.decision import decide
from opaca.policy.engine import PolicyContext
from opaca.policy.partial_fill import assess_partial_fill_safety

from tests.helpers import DEFAULT_NOW, ENGINE, evaluate, make_context, make_order, make_proposal

PRICE = Decimal("100.00")
PRICES = {"SGOV": PRICE, "BIL": PRICE, "SHV": PRICE}

UNSAFE_ASSESSMENT = PartialFillAssessment(
    safe=False,
    subsets_evaluated=3,
    violations=("filled subset [0:SGOV]: CHECK-02 worst projected liquidity < reserve",),
    zero_fill_covers_obligations=False,
)


def position(symbol: str, quantity: str) -> Position:
    qty = Decimal(quantity)
    return Position(symbol=symbol, quantity=qty, quantity_available=qty, market_value=qty * PRICE)


def _passing_decision_context() -> tuple[Proposal, PolicyContext, PolicyDecision]:
    context = make_context(prices=PRICES, obligations=(), operating_reserve=Decimal("0"))
    proposal = make_proposal(
        "prop-passes", [make_order("prop-passes", 0, "SGOV", Side.BUY, "1", PRICE)]
    )
    return proposal, context, evaluate(proposal, context)


class TestUnsafeAssessmentIsWiredBeforeAuto:
    def test_base_pass_plus_unsafe_assessment_is_reject(self) -> None:
        proposal, context, decision = _passing_decision_context()
        assert decision.passed
        authority = decide_authority(
            proposal,
            decision,
            context.authority_policy,
            context.autonomous_history,
            context.execution.now,
            partial_fill=UNSAFE_ASSESSMENT,
        )
        assert authority.result is AuthorityResult.REJECT
        assert any("partial-fill" in reason for reason in authority.reasons)

    def test_unsafe_assessment_reasons_are_carried_into_the_reject(self) -> None:
        proposal, context, decision = _passing_decision_context()
        authority = decide_authority(
            proposal,
            decision,
            context.authority_policy,
            context.autonomous_history,
            context.execution.now,
            partial_fill=UNSAFE_ASSESSMENT,
        )
        assert any("CHECK-02" in reason for reason in authority.reasons)

    def test_human_approval_cannot_override_partial_fill_reject(self) -> None:
        proposal, context, decision = _passing_decision_context()
        authority = decide_authority(
            proposal,
            decision,
            context.authority_policy,
            context.autonomous_history,
            context.execution.now,
            partial_fill=UNSAFE_ASSESSMENT,
        )
        assert not authority.can_be_approved_by_human
        assert apply_human_approval(authority).result is AuthorityResult.REJECT

    def test_decide_authority_without_assessment_fails_closed(self) -> None:
        """An unassessed proposal can never be AUTO."""
        proposal, context, decision = _passing_decision_context()
        authority = decide_authority(
            proposal,
            decision,
            context.authority_policy,
            context.autonomous_history,
            context.execution.now,
        )
        assert authority.result is AuthorityResult.REJECT
        assert any("not assessed" in reason for reason in authority.reasons)


class TestIntegrationNeverAuto:
    def test_base_pass_plus_unenumerable_subsets_is_never_auto(self) -> None:
        """Base proposal PASS + partial-fill assessment UNSAFE => never AUTO.
        Thirteen policy-valid legs pass the base evaluation but exceed the
        exhaustive-enumeration bound, so the assessment fails closed."""
        authority_policy = AuthorityPolicy(
            per_order_notional_max=Decimal("25000"),
            per_proposal_notional_max=Decimal("25000"),
            rolling_24h_notional_max=Decimal("50000"),
            rolling_order_count_max=20,
            runaway_hourly_order_count_max=20,
        )
        context = make_context(
            prices=PRICES,
            obligations=(),
            operating_reserve=Decimal("0"),
            authority_policy=authority_policy,
        )
        legs = [make_order("prop-many", i, "SGOV", Side.BUY, "1", PRICE) for i in range(13)]
        proposal = make_proposal("prop-many", legs)
        decision = evaluate(proposal, context)
        assert decision.passed
        assessment = assess_partial_fill_safety(proposal, context, ENGINE)
        assert not assessment.safe
        assert assess_partial_fill_safety(proposal, context, ENGINE).subsets_evaluated == 0
        authority = decide(proposal, context)
        assert authority.result is AuthorityResult.REJECT
        assert any("partial-fill" in reason for reason in authority.reasons)

    def test_zero_fill_unsafe_liquidation_is_never_auto(self) -> None:
        """A liquidation whose zero-fill state leaves the obligation
        uncovered is assessed UNSAFE; the authority path never AUTOs it."""
        obligation = Obligation(
            obligation_id="tax",
            name="tax payment",
            amount=Decimal("10000.00"),
            due_date=date(2026, 9, 8),
        )
        small_broker = BrokerCashState(
            cash=Decimal("500.00"),
            buying_power=Decimal("2000.00"),
            non_marginable_buying_power=Decimal("500.00"),
            multiplier=Decimal("4"),
            as_of=DEFAULT_NOW,
        )
        context = make_context(
            prices=PRICES,
            positions=(position("SGOV", "100"),),
            obligations=(obligation,),
            operating_reserve=Decimal("0"),
            broker=small_broker,
        )
        proposal = make_proposal(
            "prop-liquidate", [make_order("prop-liquidate", 0, "SGOV", Side.SELL, "100", PRICE)]
        )
        assessment = assess_partial_fill_safety(proposal, context, ENGINE)
        assert not assessment.safe
        assert decide(proposal, context).result is AuthorityResult.REJECT

    def test_safe_partial_fill_still_reaches_auto(self) -> None:
        context = make_context(
            prices=PRICES,
            positions=(position("BIL", "150"), position("SHV", "150")),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-safe-auto", [make_order("prop-safe-auto", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        assert evaluate(proposal, context).passed
        assert assess_partial_fill_safety(proposal, context, ENGINE).safe
        assert decide(proposal, context).result is AuthorityResult.AUTO


class TestNoRecursion:
    def test_base_engine_remains_separately_callable_by_subset_evaluator(self) -> None:
        """The subset evaluator calls engine.evaluate on sub-proposals; the
        base evaluation must not itself invoke partial-fill assessment. The
        full proposal passes CHECK-02 (proceeds bridge the gap); the empty
        (zero-fill) subset does not."""
        obligation = Obligation(
            obligation_id="tax",
            name="tax payment",
            amount=Decimal("10000.00"),
            due_date=date(2026, 9, 8),
        )
        small_broker = BrokerCashState(
            cash=Decimal("500.00"),
            buying_power=Decimal("2000.00"),
            non_marginable_buying_power=Decimal("500.00"),
            multiplier=Decimal("4"),
            as_of=DEFAULT_NOW,
        )
        context = make_context(
            prices=PRICES,
            positions=(position("SGOV", "100"),),
            obligations=(obligation,),
            operating_reserve=Decimal("0"),
            broker=small_broker,
        )
        proposal = make_proposal(
            "prop-recursion", [make_order("prop-recursion", 0, "SGOV", Side.SELL, "100", PRICE)]
        )
        decision = ENGINE.evaluate(proposal, context)
        assert decision.result_for(CheckId.CHECK_02).passed
        zero_fill = make_proposal("prop-recursion", [])
        sub_decision = ENGINE.evaluate(zero_fill, context)
        assert not sub_decision.result_for(CheckId.CHECK_02).passed
