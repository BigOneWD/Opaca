"""Authority engine.

Required proofs 9, 10, 11: order splitting cannot bypass the per-proposal
limit; rolling 24-hour notional and order-count limits work. Plus REJECT
precedence and the rule that human approval never overrides a hard REJECT.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from opaca.authority.engine import (
    ROLLING_NOTIONAL_WINDOW,
    apply_human_approval,
    decide_authority,
    executions_in_window,
)
from opaca.domain.models import (
    AuthorityPolicy,
    AuthorityResult,
    AutonomousExecution,
    CheckId,
    PartialFillAssessment,
    ProposedOrder,
    Side,
)

from tests.helpers import (
    DEFAULT_NOW,
    evaluate,
    make_context,
    make_order,
    make_proposal,
)

#: These tests isolate the authority dimensions; partial-fill safety is a
#: separate gate (tested in test_partial_fill_authority.py) and is asserted
#: SAFE explicitly here.
SAFE_PARTIAL_FILL = PartialFillAssessment(
    safe=True, subsets_evaluated=0, violations=(), zero_fill_covers_obligations=None
)

PRICE = Decimal("100.00")
PRICES = {"SGOV": PRICE, "BIL": PRICE, "SHV": PRICE}


def buy_legs(
    proposal_id: str, count: int, notional_each: Decimal, start_index: int = 0
) -> list[ProposedOrder]:
    symbols = ["SGOV", "BIL", "SHV"]
    legs = []
    for i in range(count):
        symbol = symbols[i % len(symbols)]
        legs.append(
            make_order(
                proposal_id,
                start_index + i,
                symbol,
                Side.BUY,
                str(notional_each / PRICE),
                PRICE,
            )
        )
    return legs


def split_authority_policy() -> AuthorityPolicy:
    return AuthorityPolicy(
        per_order_notional_max=Decimal("6000"),
        per_proposal_notional_max=Decimal("20000"),
        rolling_24h_notional_max=Decimal("100000"),
        rolling_order_count_max=20,
        runaway_hourly_order_count_max=20,
    )


class TestSplitOrdersCannotBypassAuthority:
    def test_split_orders_cannot_bypass_per_proposal_limit(self) -> None:
        """Required proof 9: 5 x $5,000 legs each pass the per-order limit
        but the $25,000 aggregate exceeds the per-proposal limit."""
        context = make_context(
            prices=PRICES,
            authority_policy=split_authority_policy(),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal("prop-split", buy_legs("prop-split", 5, Decimal("5000")))
        assert proposal.total_buy_notional == Decimal("25000.00")
        decision = evaluate(proposal, context)
        check_07 = decision.result_for(CheckId.CHECK_07)
        assert not check_07.passed
        assert decision.passed  # CHECK-07 is an authority input, not a hard reject
        authority = decide_authority(
            proposal,
            decision,
            context.authority_policy,
            context.autonomous_history,
            context.execution.now,
            partial_fill=SAFE_PARTIAL_FILL,
        )
        assert authority.result is AuthorityResult.APPROVAL_REQUIRED
        assert any("per-proposal" in reason for reason in authority.reasons)


class TestRollingWindows:
    def test_rolling_24h_notional_limit_enforced(self) -> None:
        """Required proof 10."""
        history = (
            AutonomousExecution(
                timestamp=DEFAULT_NOW - timedelta(hours=2), notional=Decimal("40000")
            ),
        )
        context = make_context(
            prices=PRICES,
            autonomous_history=history,
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal("prop-roll", buy_legs("prop-roll", 2, Decimal("7500")))
        decision = evaluate(proposal, context)
        authority = decide_authority(
            proposal,
            decision,
            context.authority_policy,
            context.autonomous_history,
            context.execution.now,
            partial_fill=SAFE_PARTIAL_FILL,
        )
        assert authority.result is AuthorityResult.APPROVAL_REQUIRED
        assert any("rolling 24h" in reason for reason in authority.reasons)

    def test_rolling_24h_window_excludes_old_executions(self) -> None:
        history = (
            AutonomousExecution(
                timestamp=DEFAULT_NOW - timedelta(hours=25), notional=Decimal("40000")
            ),
        )
        context = make_context(
            prices=PRICES,
            autonomous_history=history,
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal("prop-roll-2", buy_legs("prop-roll-2", 2, Decimal("7500")))
        decision = evaluate(proposal, context)
        authority = decide_authority(
            proposal,
            decision,
            context.authority_policy,
            context.autonomous_history,
            context.execution.now,
            partial_fill=SAFE_PARTIAL_FILL,
        )
        assert authority.result is AuthorityResult.AUTO

    def test_rolling_order_count_limit_enforced(self) -> None:
        """Required proof 11."""
        history = tuple(
            AutonomousExecution(timestamp=DEFAULT_NOW - timedelta(hours=1), notional=Decimal("100"))
            for _ in range(9)
        )
        context = make_context(
            prices=PRICES,
            autonomous_history=history,
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal("prop-count", buy_legs("prop-count", 2, Decimal("1000")))
        decision = evaluate(proposal, context)
        authority = decide_authority(
            proposal,
            decision,
            context.authority_policy,
            context.autonomous_history,
            context.execution.now,
            partial_fill=SAFE_PARTIAL_FILL,
        )
        assert authority.result is AuthorityResult.APPROVAL_REQUIRED
        assert any("order count" in reason for reason in authority.reasons)

    def test_exactly_24h_old_remains_excluded(self) -> None:
        history = (
            AutonomousExecution(
                timestamp=DEFAULT_NOW - timedelta(hours=24), notional=Decimal("40000")
            ),
        )
        counted = executions_in_window(history, DEFAULT_NOW, ROLLING_NOTIONAL_WINDOW)
        assert counted == ()

    def test_just_inside_24h_cutoff_remains_included(self) -> None:
        history = (
            AutonomousExecution(
                timestamp=DEFAULT_NOW - timedelta(hours=23, minutes=59, seconds=59),
                notional=Decimal("40000"),
            ),
        )
        counted = executions_in_window(history, DEFAULT_NOW, ROLLING_NOTIONAL_WINDOW)
        assert counted == history

    def test_exactly_now_counts_in_the_window(self) -> None:
        history = (AutonomousExecution(timestamp=DEFAULT_NOW, notional=Decimal("40000")),)
        counted = executions_in_window(history, DEFAULT_NOW, ROLLING_NOTIONAL_WINDOW)
        assert counted == history

    def test_one_second_before_now_counts_in_the_window(self) -> None:
        history = (
            AutonomousExecution(
                timestamp=DEFAULT_NOW - timedelta(seconds=1), notional=Decimal("40000")
            ),
        )
        counted = executions_in_window(history, DEFAULT_NOW, ROLLING_NOTIONAL_WINDOW)
        assert counted == history

    def test_one_second_after_now_counts_in_the_window(self) -> None:
        history = (
            AutonomousExecution(
                timestamp=DEFAULT_NOW + timedelta(seconds=1), notional=Decimal("40000")
            ),
        )
        counted = executions_in_window(history, DEFAULT_NOW, ROLLING_NOTIONAL_WINDOW)
        assert counted == history

    def test_future_timestamp_cannot_increase_autonomous_authority(self) -> None:
        history = (
            AutonomousExecution(
                timestamp=DEFAULT_NOW + timedelta(seconds=1), notional=Decimal("40000")
            ),
        )
        context = make_context(
            prices=PRICES,
            autonomous_history=history,
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal("prop-future", buy_legs("prop-future", 2, Decimal("7500")))
        decision = evaluate(proposal, context)
        authority = decide_authority(
            proposal,
            decision,
            context.authority_policy,
            context.autonomous_history,
            context.execution.now,
            partial_fill=SAFE_PARTIAL_FILL,
        )
        assert authority.result is AuthorityResult.APPROVAL_REQUIRED
        assert any("rolling 24h" in reason for reason in authority.reasons)

    def test_rolling_order_count_at_limit_is_auto(self) -> None:
        history = tuple(
            AutonomousExecution(timestamp=DEFAULT_NOW - timedelta(hours=1), notional=Decimal("100"))
            for _ in range(8)
        )
        context = make_context(
            prices=PRICES,
            autonomous_history=history,
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal("prop-count-2", buy_legs("prop-count-2", 2, Decimal("1000")))
        decision = evaluate(proposal, context)
        authority = decide_authority(
            proposal,
            decision,
            context.authority_policy,
            context.autonomous_history,
            context.execution.now,
            partial_fill=SAFE_PARTIAL_FILL,
        )
        assert authority.result is AuthorityResult.AUTO


class TestDecisionPrecedence:
    def test_hard_policy_violation_is_reject_not_approval_required(self) -> None:
        context = make_context(
            prices=PRICES,
            authority_policy=split_authority_policy(),
            obligations=(),
            operating_reserve=Decimal("0"),
            kill_switch=True,
        )
        proposal = make_proposal("prop-reject", buy_legs("prop-reject", 5, Decimal("5000")))
        decision = evaluate(proposal, context)
        authority = decide_authority(
            proposal,
            decision,
            context.authority_policy,
            context.autonomous_history,
            context.execution.now,
            partial_fill=SAFE_PARTIAL_FILL,
        )
        assert authority.result is AuthorityResult.REJECT

    def test_reject_is_returned_for_hard_violations_even_if_authority_exceeded(self) -> None:
        context = make_context(prices=PRICES, obligations=(), operating_reserve=Decimal("0"))
        proposal = make_proposal(
            "prop-bad",
            [make_order("prop-bad", 0, "AAPL", Side.BUY, "1000", PRICE)],
        )
        decision = evaluate(proposal, context)
        assert not decision.passed
        authority = decide_authority(
            proposal,
            decision,
            context.authority_policy,
            context.autonomous_history,
            context.execution.now,
            partial_fill=SAFE_PARTIAL_FILL,
        )
        assert authority.result is AuthorityResult.REJECT


class TestHumanApproval:
    def test_approval_promotes_only_approval_required(self) -> None:
        context = make_context(
            prices=PRICES,
            authority_policy=split_authority_policy(),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal("prop-appr", buy_legs("prop-appr", 5, Decimal("5000")))
        decision = evaluate(proposal, context)
        authority = decide_authority(
            proposal,
            decision,
            context.authority_policy,
            context.autonomous_history,
            context.execution.now,
            partial_fill=SAFE_PARTIAL_FILL,
        )
        assert authority.result is AuthorityResult.APPROVAL_REQUIRED
        promoted = apply_human_approval(authority)
        assert promoted.result is AuthorityResult.AUTO

    def test_human_approval_can_never_override_reject(self) -> None:
        context = make_context(prices=PRICES, obligations=(), operating_reserve=Decimal("0"))
        proposal = make_proposal(
            "prop-veto",
            [make_order("prop-veto", 0, "AAPL", Side.BUY, "10", PRICE)],
        )
        decision = evaluate(proposal, context)
        authority = decide_authority(
            proposal,
            decision,
            context.authority_policy,
            context.autonomous_history,
            context.execution.now,
            partial_fill=SAFE_PARTIAL_FILL,
        )
        assert authority.result is AuthorityResult.REJECT
        assert not authority.can_be_approved_by_human
        assert apply_human_approval(authority).result is AuthorityResult.REJECT
