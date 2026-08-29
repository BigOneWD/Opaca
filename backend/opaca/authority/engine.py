"""Authority engine (SPEC s9 CHECK-07, s10 approval model).

Result is exactly one of AUTO / APPROVAL_REQUIRED / REJECT:

* REJECT              - hard policy violation (kill switch included), or a
                        partial-fill safety assessment that is not SAFE
                        (RT-06: a hard safety failure, never AUTO, and not
                        something human approval silently overrides).
* AUTO                - policy-valid AND partial-fill-safe AND every
                        delegated dimension passes.
* APPROVAL_REQUIRED   - policy-valid but outside delegated authority.

Splitting one action across several orders cannot bypass authority: the
per-proposal aggregate covers intra-proposal splits and the rolling windows
cover cross-proposal splits. Human approval never overrides REJECT.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from opaca.domain.models import (
    AuthorityDecision,
    AuthorityPolicy,
    AuthorityResult,
    AutonomousExecution,
    PartialFillAssessment,
    PolicyDecision,
    Proposal,
)
from opaca.domain.money import ZERO

ROLLING_NOTIONAL_WINDOW = timedelta(hours=24)
ROLLING_COUNT_WINDOW = timedelta(hours=24)
RUNAWAY_WINDOW = timedelta(hours=1)


def executions_in_window(
    history: Sequence[AutonomousExecution], now: datetime, window: timedelta
) -> tuple[AutonomousExecution, ...]:
    cutoff = now - window
    return tuple(e for e in history if cutoff < e.timestamp <= now)


def rolling_notional(
    history: Sequence[AutonomousExecution],
    now: datetime,
    window: timedelta = ROLLING_NOTIONAL_WINDOW,
) -> Decimal:
    return sum((e.notional for e in executions_in_window(history, now, window)), ZERO)


def rolling_count(
    history: Sequence[AutonomousExecution], now: datetime, window: timedelta = ROLLING_COUNT_WINDOW
) -> int:
    return len(executions_in_window(history, now, window))


def authority_dimension_violations(
    proposal: Proposal,
    policy: AuthorityPolicy,
    history: Sequence[AutonomousExecution],
    now: datetime,
) -> tuple[str, ...]:
    """The four CHECK-07 delegated-authority dimensions."""
    violations: list[str] = []
    for leg in proposal.legs:
        if leg.notional > policy.per_order_notional_max:
            violations.append(
                f"per-order notional {leg.notional} exceeds limit "
                f"{policy.per_order_notional_max} (leg {leg.leg_index} {leg.symbol})"
            )
    aggregate = proposal.total_buy_notional + proposal.total_sell_notional
    if aggregate > policy.per_proposal_notional_max:
        violations.append(
            f"per-proposal aggregate notional {aggregate} exceeds limit "
            f"{policy.per_proposal_notional_max}"
        )
    projected_notional = rolling_notional(history, now) + aggregate
    if projected_notional > policy.rolling_24h_notional_max:
        violations.append(
            f"rolling 24h autonomous notional {projected_notional} exceeds limit "
            f"{policy.rolling_24h_notional_max}"
        )
    projected_count = rolling_count(history, now) + len(proposal.legs)
    if projected_count > policy.rolling_order_count_max:
        violations.append(
            f"rolling autonomous order count {projected_count} exceeds limit "
            f"{policy.rolling_order_count_max}"
        )
    return tuple(violations)


def runaway_order_count_violation(
    proposal: Proposal,
    policy: AuthorityPolicy,
    history: Sequence[AutonomousExecution],
    now: datetime,
) -> str | None:
    """CHECK-13: maximum autonomous orders per rolling hour (hard limit)."""
    projected = rolling_count(history, now, RUNAWAY_WINDOW) + len(proposal.legs)
    if projected > policy.runaway_hourly_order_count_max:
        return (
            f"runaway limit: {projected} autonomous orders in the rolling hour "
            f"exceeds maximum {policy.runaway_hourly_order_count_max}"
        )
    return None


def decide_authority(
    proposal: Proposal,
    policy_decision: PolicyDecision,
    authority_policy: AuthorityPolicy,
    history: Sequence[AutonomousExecution],
    now: datetime,
    partial_fill: PartialFillAssessment | None = None,
) -> AuthorityDecision:
    """AUTO is reachable only with a SAFE partial-fill assessment (RT-06).

    ``partial_fill=None`` fails closed: an unassessed proposal can never be
    AUTO. An UNSAFE assessment is a hard safety failure — REJECT — that
    human approval cannot override; it is never downgraded to
    APPROVAL_REQUIRED.
    """
    if not policy_decision.passed:
        reasons = tuple(f"{r.check_id.value}: {r.detail}" for r in policy_decision.violations) or (
            "hard policy violation",
        )
        return AuthorityDecision(
            result=AuthorityResult.REJECT,
            reasons=reasons,
            policy_decision=policy_decision,
        )
    if partial_fill is None:
        return AuthorityDecision(
            result=AuthorityResult.REJECT,
            reasons=("partial-fill safety was not assessed; execution must fail closed",),
            policy_decision=policy_decision,
        )
    if not partial_fill.safe:
        return AuthorityDecision(
            result=AuthorityResult.REJECT,
            reasons=("partial-fill safety assessment is UNSAFE; hard safety failure",)
            + partial_fill.violations,
            policy_decision=policy_decision,
        )
    violations = authority_dimension_violations(proposal, authority_policy, history, now)
    if violations:
        return AuthorityDecision(
            result=AuthorityResult.APPROVAL_REQUIRED,
            reasons=violations,
            policy_decision=policy_decision,
        )
    return AuthorityDecision(
        result=AuthorityResult.AUTO,
        reasons=("all delegated-authority dimensions pass",),
        policy_decision=policy_decision,
    )


def apply_human_approval(decision: AuthorityDecision) -> AuthorityDecision:
    """SPEC s10: approval only promotes APPROVAL_REQUIRED. A hard policy
    REJECT can never be overridden by human approval; policy must run again
    before any submission."""
    if decision.result is AuthorityResult.APPROVAL_REQUIRED:
        return AuthorityDecision(
            result=AuthorityResult.AUTO,
            reasons=decision.reasons
            + ("human approval granted; policy MUST re-run before submission",),
            policy_decision=decision.policy_decision,
        )
    return decision
