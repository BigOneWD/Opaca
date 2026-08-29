"""Composite authority decision for the treasury core.

The authority path is: evaluate -> partial-fill safety -> authority
(RT-06). A proposal whose base TreasuryGuard evaluation passes but whose
partial-fill assessment is UNSAFE is a hard safety failure and can never
become AUTO; the assessment is wired into ``decide_authority`` before any
AUTO is reachable.

No recursion: the base ``TreasuryGuardEngine.evaluate`` remains separately
callable and is what the subset evaluator uses internally; this module is
the only place that composes the full authority decision.
"""

from __future__ import annotations

from opaca.authority.engine import decide_authority
from opaca.domain.models import AuthorityDecision, Proposal
from opaca.policy.engine import PolicyContext, TreasuryGuardEngine
from opaca.policy.partial_fill import assess_partial_fill_safety


def decide(
    proposal: Proposal,
    context: PolicyContext,
    engine: TreasuryGuardEngine | None = None,
) -> AuthorityDecision:
    evaluator = engine if engine is not None else TreasuryGuardEngine()
    decision = evaluator.evaluate(proposal, context)
    if not decision.passed:
        return decide_authority(
            proposal,
            decision,
            context.authority_policy,
            context.autonomous_history,
            context.execution.now,
        )
    assessment = assess_partial_fill_safety(proposal, context, evaluator)
    return decide_authority(
        proposal,
        decision,
        context.authority_policy,
        context.autonomous_history,
        context.execution.now,
        partial_fill=assessment,
    )
