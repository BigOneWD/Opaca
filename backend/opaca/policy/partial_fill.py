"""Partial-fill safety modeling (SPEC s12), deterministic domain behavior.

Policy cannot assume all legs fill together. Every relevant non-empty subset
of the proposal's legs — buy legs AND sell legs alike — is evaluated through
the applicable hard controls (RT-09):

* BUY subsets: funding and concentration controls must hold when only the
  subset fills. Under the Amendment G investment-pool-base denominator the
  pool is fixed at proposal evaluation time, so unfilled investment cash
  stays in the pool and a single-leg fill never shows a fake 100%
  concentration; the enumeration remains the mechanism that proves it.

* SELL subsets: concentration changes caused by sell subsets are included,
  and proposed liquidation proceeds are never assumed available. Coverage is
  evaluated for every fill subset including the empty (zero-fill) subset;
  ``zero_fill_covers_obligations=False`` means runtime must never represent
  liquidity as restored until actual fills reconcile and settle on the
  derived schedule.

Mixed subsets inherit the proposal's legs as a whole, so checks that govern
the proposal regardless of which legs fill (e.g. CHECK-10 opposing sides)
are answered by the base evaluation and are not re-run per subset. Buy
notional is conservatively assumed spent for coverage arithmetic: a fill
outcome without the buys is never worse for obligations than one with them.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from itertools import combinations

from opaca.domain.models import (
    CheckId,
    PartialFillAssessment,
    PolicyDecision,
    Proposal,
    ProposedOrder,
    Side,
)
from opaca.domain.money import ZERO
from opaca.policy.engine import PolicyContext, TreasuryGuardEngine
from opaca.treasury.liquidity import (
    LiquidityProjection,
    compute_liquidity,
    sell_settlement_events,
)

#: Hackathon proposals are small; exhaustive enumeration is acceptable.
MAX_ENUMERATED_LEGS = 12

#: Hard controls applied to every fill subset. CHECK-04 is included for sell
#: subsets as well (RT-09); CHECK-16 guards sell subsets against oversell.
PARTIAL_FILL_CHECKS = frozenset(
    {
        CheckId.CHECK_01,
        CheckId.CHECK_02,
        CheckId.CHECK_04,
        CheckId.CHECK_06,
        CheckId.CHECK_11,
        CheckId.CHECK_16,
    }
)

__all__ = [
    "MAX_ENUMERATED_LEGS",
    "PARTIAL_FILL_CHECKS",
    "PartialFillAssessment",
    "assess_partial_fill_safety",
]


def _subsets(legs: tuple[ProposedOrder, ...]) -> tuple[tuple[ProposedOrder, ...], ...]:
    result: list[tuple[ProposedOrder, ...]] = []
    for size in range(1, len(legs) + 1):
        result.extend(combinations(legs, size))
    return tuple(result)


def _unsafe(subsets_evaluated: int, violations: tuple[str, ...]) -> PartialFillAssessment:
    return PartialFillAssessment(
        safe=False,
        subsets_evaluated=subsets_evaluated,
        violations=violations,
        zero_fill_covers_obligations=None,
    )


def assess_partial_fill_safety(
    proposal: Proposal,
    context: PolicyContext,
    engine: TreasuryGuardEngine,
) -> PartialFillAssessment:
    """Exhaustive subset assessment. The base TreasuryGuard evaluation stays
    separately callable: this function invokes ``engine.evaluate`` on subset
    proposals only, so no recursion exists (RT-06)."""
    legs = proposal.legs
    buy_legs = proposal.buy_legs
    sell_legs = proposal.sell_legs
    if len(buy_legs) > MAX_ENUMERATED_LEGS:
        return _unsafe(
            0,
            (
                f"too many buy legs ({len(buy_legs)}) to enumerate partial-fill "
                f"subsets; fail closed",
            ),
        )
    if len(legs) > MAX_ENUMERATED_LEGS:
        return _unsafe(
            0,
            (f"too many legs ({len(legs)}) to enumerate partial-fill subsets; fail closed",),
        )

    violations: list[str] = []
    subsets_evaluated = 0
    coverage = _SellCoverage(context, proposal) if sell_legs else None
    zero_fill_covers = coverage.covers_with_subset(()) if coverage is not None else None

    for subset in _subsets(legs):
        subsets_evaluated += 1
        leg_names = ", ".join(f"{leg.leg_index}:{leg.symbol}" for leg in subset)
        sub_proposal = Proposal(proposal_id=proposal.proposal_id, legs=tuple(subset))
        decision: PolicyDecision = engine.evaluate(sub_proposal, context, only=PARTIAL_FILL_CHECKS)
        for result in decision.violations:
            violations.append(
                f"filled subset [{leg_names}]: {result.check_id.value} {result.detail}"
            )
        if coverage is not None:
            sell_subset = tuple(leg for leg in subset if leg.side is Side.SELL)
            if not coverage.covers_with_subset(sell_subset):
                sell_names = ", ".join(f"{leg.leg_index}:{leg.symbol}" for leg in sell_subset)
                violations.append(
                    f"sell subset [{sell_names}] leaves obligations uncovered on the "
                    f"derived settlement schedule"
                )

    if zero_fill_covers is False:
        violations.append(
            "zero-fill condition does not cover obligations: liquidity MUST NOT "
            "be represented as restored until actual fills reconcile and settle"
        )

    return PartialFillAssessment(
        safe=not violations,
        subsets_evaluated=subsets_evaluated,
        violations=tuple(violations),
        zero_fill_covers_obligations=zero_fill_covers,
    )


class _SellCoverage:
    """Deterministic obligation-coverage arithmetic for sell-side outcomes."""

    def __init__(self, context: PolicyContext, proposal: Proposal) -> None:
        as_of = context.execution.now.date()
        self._liquidity: LiquidityProjection = compute_liquidity(
            broker=context.broker,
            obligations=context.obligations,
            settlement_events=context.settlement_events,
            operating_reserve=context.liquidity_policy.operating_reserve,
            as_of=as_of,
        )
        self._buy_notional = proposal.total_buy_notional
        sell_events = sell_settlement_events(proposal.sell_legs, as_of, context.calendar)
        self._event_by_leg = {
            leg.leg_index: event for leg, event in zip(proposal.sell_legs, sell_events, strict=True)
        }

    def _available_on(self, day: date, proceeds: Decimal) -> Decimal:
        liquidity = self._liquidity
        return (
            liquidity.settled_cash
            - self._buy_notional
            + liquidity.proceeds_settling_by(day)
            + proceeds
            - liquidity.obligations_due_by(day)
        )

    def covers_with_subset(self, subset: tuple[ProposedOrder, ...]) -> bool:
        """True if every obligation is covered when ONLY ``subset`` fills."""
        for obligation in self._liquidity.obligations:
            proceeds = ZERO
            for leg in subset:
                event = self._event_by_leg[leg.leg_index]
                if event.settlement_date <= obligation.due_date:
                    proceeds += event.amount
            if self._available_on(obligation.due_date, proceeds) < ZERO:
                return False
        return True
