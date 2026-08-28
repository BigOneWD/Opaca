"""Partial-fill safety modeling (SPEC s12), deterministic domain behavior.

Policy cannot assume all legs fill together:

* Multi-leg BUYS: every non-empty subset of the buy legs is evaluated in
  isolation. A subset that fills alone must not create prohibited
  concentration (CHECK-04) or funding violations (CHECK-01/02/06/11). A
  single leg filling alone concentrates the whole filled amount in one
  symbol, so diversification plans must survive each leg filling alone.

* SELLS: proposed liquidation proceeds must not be assumed available. The
  assessment reports whether obligations remain covered with ZERO fills; if
  not, runtime must never represent liquidity as restored until actual fills
  reconcile and settle on the derived schedule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import combinations

from opaca.domain.models import CheckId, Proposal, ProposedOrder
from opaca.domain.money import ZERO
from opaca.policy.engine import PolicyContext, TreasuryGuardEngine
from opaca.treasury.liquidity import (
    LiquidityProjection,
    compute_liquidity,
    sell_settlement_events,
)

MAX_ENUMERATED_LEGS = 12

BUY_SAFETY_CHECKS = frozenset(
    {
        CheckId.CHECK_01,
        CheckId.CHECK_02,
        CheckId.CHECK_04,
        CheckId.CHECK_06,
        CheckId.CHECK_11,
    }
)


@dataclass(frozen=True)
class PartialFillAssessment:
    safe: bool
    subsets_evaluated: int
    violations: tuple[str, ...]
    zero_fill_covers_obligations: bool | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "violations", tuple(self.violations))


def _subsets(legs: tuple[ProposedOrder, ...]) -> tuple[tuple[ProposedOrder, ...], ...]:
    result: list[tuple[ProposedOrder, ...]] = []
    for size in range(1, len(legs) + 1):
        result.extend(combinations(legs, size))
    return tuple(result)


def assess_partial_fill_safety(
    proposal: Proposal,
    context: PolicyContext,
    engine: TreasuryGuardEngine,
) -> PartialFillAssessment:
    violations: list[str] = []
    subsets_evaluated = 0

    buy_legs = proposal.buy_legs
    if len(buy_legs) > MAX_ENUMERATED_LEGS:
        return PartialFillAssessment(
            safe=False,
            subsets_evaluated=0,
            violations=(
                f"too many buy legs ({len(buy_legs)}) to enumerate partial-fill "
                f"subsets; fail closed",
            ),
            zero_fill_covers_obligations=None,
        )
    for subset in _subsets(buy_legs):
        subsets_evaluated += 1
        sub_proposal = Proposal(proposal_id=proposal.proposal_id, legs=tuple(subset))
        decision = engine.evaluate(sub_proposal, context, only=BUY_SAFETY_CHECKS)
        for result in decision.violations:
            leg_names = ", ".join(f"{leg.leg_index}:{leg.symbol}" for leg in subset)
            violations.append(
                f"filled subset [{leg_names}]: {result.check_id.value} {result.detail}"
            )

    zero_fill_covers: bool | None = None
    sell_legs = proposal.sell_legs
    if sell_legs:
        if len(sell_legs) > MAX_ENUMERATED_LEGS:
            return PartialFillAssessment(
                safe=False,
                subsets_evaluated=subsets_evaluated,
                violations=tuple(violations)
                + (
                    f"too many sell legs ({len(sell_legs)}) to enumerate partial-fill "
                    f"subsets; fail closed",
                ),
                zero_fill_covers_obligations=None,
            )
        coverage = _SellCoverage(context, proposal)
        zero_fill_covers = coverage.covers_with_subset(())
        for subset in _subsets(sell_legs):
            subsets_evaluated += 1
            if not coverage.covers_with_subset(subset):
                leg_names = ", ".join(f"{leg.leg_index}:{leg.symbol}" for leg in subset)
                violations.append(
                    f"sell subset [{leg_names}] leaves obligations uncovered on the "
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
            leg.leg_index: event for leg, event in zip(proposal.sell_legs, sell_events)
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
