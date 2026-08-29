"""Scenario initialization (SPEC s3/s4, Amendment A; Phase -1A decision).

Ratios are converted to absolute amounts ONCE at seeding time from the
reconciled opening broker cash. After seeding:

* obligations remain absolute values,
* the operating reserve remains the policy-defined absolute amount,
* later broker cash movements MUST NOT rescale seeded obligations.

The treasury engines therefore always receive this immutable seed plus the
current broker cash state; no code path rescales the seed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal

from opaca.domain.models import Obligation
from opaca.domain.money import ZERO, money, positive_money, round_money

#: Frozen demo ratios (Phase -1A approved decision; sums to exactly 1).
PAYROLL_RATIO = Decimal("0.24")
SUPPLIERS_RATIO = Decimal("0.14")
OPERATING_RESERVE_RATIO = Decimal("0.40")
INVESTABLE_SURPLUS_RATIO = Decimal("0.22")

#: Relative offsets exist ONLY here, at seeding time (SPEC s4). Seeded
#: due-dates are concrete ISO dates: payroll +10 days, suppliers +18 days,
#: mirroring the SPEC s4 illustrative baseline (+10 / +18).
PAYROLL_OFFSET_DAYS = 10
SUPPLIERS_OFFSET_DAYS = 18

PAYROLL_OBLIGATION_ID = "seed-payroll"
SUPPLIERS_OBLIGATION_ID = "seed-suppliers"


@dataclass(frozen=True)
class ScenarioSeed:
    """Immutable result of one-time scenario initialization."""

    opening_cash: Decimal
    seeded_at: date
    obligations: tuple[Obligation, ...]
    operating_reserve: Decimal
    investable_surplus: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "obligations", tuple(self.obligations))

    @property
    def obligations_total(self) -> Decimal:
        return sum((o.amount for o in self.obligations), ZERO)


def seed_scenario(
    opening_cash: Decimal | str | int,
    seeded_at: date,
    payroll_ratio: Decimal = PAYROLL_RATIO,
    suppliers_ratio: Decimal = SUPPLIERS_RATIO,
    reserve_ratio: Decimal = OPERATING_RESERVE_RATIO,
) -> ScenarioSeed:
    """Convert ratios to absolute amounts once. Each seeded amount is rounded
    DOWN to cents so it never exceeds its ratio of opening cash; the
    investable surplus is the exact residual, so the parts always sum to the
    opening cash."""
    cash = positive_money(opening_cash)
    payroll_amount = round_money(money(payroll_ratio) * cash, ROUND_DOWN)
    suppliers_amount = round_money(money(suppliers_ratio) * cash, ROUND_DOWN)
    reserve_amount = round_money(money(reserve_ratio) * cash, ROUND_DOWN)
    investable = cash - payroll_amount - suppliers_amount - reserve_amount
    if investable < ZERO:
        raise ValueError("ratios exceed 100% of opening cash")
    obligations = (
        Obligation(
            obligation_id=PAYROLL_OBLIGATION_ID,
            name="payroll",
            amount=payroll_amount,
            due_date=seeded_at + timedelta(days=PAYROLL_OFFSET_DAYS),
        ),
        Obligation(
            obligation_id=SUPPLIERS_OBLIGATION_ID,
            name="suppliers",
            amount=suppliers_amount,
            due_date=seeded_at + timedelta(days=SUPPLIERS_OFFSET_DAYS),
        ),
    )
    return ScenarioSeed(
        opening_cash=cash,
        seeded_at=seeded_at,
        obligations=obligations,
        operating_reserve=reserve_amount,
        investable_surplus=investable,
    )
