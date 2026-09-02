"""RED-phase contracts for delegated Wheel authority."""

from __future__ import annotations

from decimal import Decimal

from opaca.domain.models import AuthorityResult, PolicyDecision
from opaca.wheel.authority import WheelAuthorityContext, decide_wheel_authority


def context(
    *,
    proposed: str = "8000",
    post_name: str = "8000",
    aggregate: str = "8000",
    hard_policy_passed: bool = True,
    stricter_limits_passed: bool = True,
) -> WheelAuthorityContext:
    return WheelAuthorityContext(
        risk_capital_base=Decimal("100000"),
        proposed_assignment_capital=Decimal(proposed),
        post_trade_underlying_exposure=Decimal(post_name),
        post_trade_aggregate_exposure=Decimal(aggregate),
        policy_decision=PolicyDecision(passed=hard_policy_passed, results=()),
        stricter_limits_passed=stricter_limits_passed,
    )


def test_delegated_envelope_is_auto_at_eight_percent() -> None:
    decision = decide_wheel_authority(context())

    assert decision.result is AuthorityResult.AUTO


def test_aggregate_above_twenty_percent_requires_approval() -> None:
    decision = decide_wheel_authority(context(aggregate="21000"))

    assert decision.result is AuthorityResult.APPROVAL_REQUIRED


def test_proposal_above_ten_percent_but_below_hard_cap_requires_approval() -> None:
    decision = decide_wheel_authority(
        context(proposed="12000", post_name="12000", aggregate="12000")
    )

    assert decision.result is AuthorityResult.APPROVAL_REQUIRED


def test_hard_policy_failure_is_reject_and_not_human_overridable() -> None:
    decision = decide_wheel_authority(context(hard_policy_passed=False))

    assert decision.result is AuthorityResult.REJECT
    assert decision.can_be_approved_by_human is False


def test_stricter_rolling_or_runaway_limit_can_only_tighten_authority() -> None:
    decision = decide_wheel_authority(context(stricter_limits_passed=False))

    assert decision.result is AuthorityResult.APPROVAL_REQUIRED

