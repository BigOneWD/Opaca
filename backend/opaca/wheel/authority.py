"""Delegated authority and approval validation for the Competition Wheel."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from opaca.domain.models import AuthorityDecision, AuthorityResult, PolicyDecision
from opaca.domain.money import non_negative_money, positive_money
from opaca.wheel.config import WheelPolicy
from opaca.wheel.models import WheelApprovalBinding

APPROVAL_TTL = timedelta(minutes=5)


@dataclass(frozen=True)
class WheelAuthorityContext:
    """Reconciled exposure inputs used for Wheel delegated authority."""

    risk_capital_base: Decimal
    proposed_assignment_capital: Decimal
    post_trade_underlying_exposure: Decimal
    post_trade_aggregate_exposure: Decimal
    policy_decision: PolicyDecision
    stricter_limits_passed: bool = True
    policy: WheelPolicy = field(default_factory=WheelPolicy)

    def __post_init__(self) -> None:
        object.__setattr__(self, "risk_capital_base", positive_money(self.risk_capital_base))
        for name in (
            "proposed_assignment_capital",
            "post_trade_underlying_exposure",
            "post_trade_aggregate_exposure",
        ):
            object.__setattr__(self, name, non_negative_money(getattr(self, name)))


def decide_wheel_authority(context: WheelAuthorityContext) -> AuthorityDecision:
    """Classify a hard-valid Wheel proposal as AUTO or approval-required.

    Existing rolling/runaway controls arrive as ``stricter_limits_passed``;
    they can narrow this delegated envelope but cannot widen it.
    """
    policy_decision = context.policy_decision
    if not policy_decision.passed:
        reasons = tuple(
            f"{result.check_id.value}: {result.detail}"
            for result in policy_decision.violations
        ) or ("hard policy violation",)
        return AuthorityDecision(
            result=AuthorityResult.REJECT,
            reasons=reasons,
            policy_decision=policy_decision,
        )

    base = context.risk_capital_base
    approval_reasons: list[str] = []
    if context.proposed_assignment_capital > base * context.policy.auto_proposal_fraction:
        approval_reasons.append("proposal exceeds the delegated 10% assignment envelope")
    if context.post_trade_underlying_exposure > base * context.policy.auto_underlying_fraction:
        approval_reasons.append("post-trade underlying exposure exceeds the delegated 10% envelope")
    if context.post_trade_aggregate_exposure > base * context.policy.auto_aggregate_fraction:
        approval_reasons.append("post-trade aggregate exposure exceeds the delegated 20% envelope")
    if not context.stricter_limits_passed:
        approval_reasons.append("a stricter rolling or runaway limit requires approval")

    if approval_reasons:
        return AuthorityDecision(
            result=AuthorityResult.APPROVAL_REQUIRED,
            reasons=tuple(approval_reasons),
            policy_decision=policy_decision,
        )
    return AuthorityDecision(
        result=AuthorityResult.AUTO,
        reasons=("all Wheel delegated-authority dimensions pass",),
        policy_decision=policy_decision,
    )

def approval_is_current(binding: WheelApprovalBinding | None, now: datetime) -> bool:
    """Return whether an approval is within its exact five-minute TTL."""
    if binding is None:
        return False
    if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(None):
        raise ValueError("now must be timezone-aware UTC")
    if binding.expires_at - binding.approved_at != APPROVAL_TTL:
        return False
    return binding.approved_at <= now < binding.expires_at


def approval_matches(
    binding: WheelApprovalBinding,
    expected: WheelApprovalBinding,
) -> bool:
    """Compare every approval field bound to the original Wheel decision."""
    return (
        binding.wheel_decision_run_id == expected.wheel_decision_run_id
        and binding.attempt_number == expected.attempt_number
        and binding.occ_symbol == expected.occ_symbol
        and binding.action is expected.action
        and binding.contracts == expected.contracts
        and binding.assignment_capital == expected.assignment_capital
        and binding.approved_sell_limit_premium == expected.approved_sell_limit_premium
    )
