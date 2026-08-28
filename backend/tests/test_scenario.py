"""Scenario initialization: ratios converted to absolute amounts ONCE."""

from __future__ import annotations

from datetime import date
from decimal import ROUND_DOWN, Decimal

import pytest
from opaca.domain.models import BrokerCashState, BrokerEnvironment, ExecutionContext
from opaca.domain.money import ZERO, round_money
from opaca.treasury.liquidity import compute_liquidity
from opaca.treasury.scenario import (
    INVESTABLE_SURPLUS_RATIO,
    OPERATING_RESERVE_RATIO,
    PAYROLL_RATIO,
    SUPPLIERS_RATIO,
    seed_scenario,
)

from tests.helpers import DEFAULT_NOW, phase1_broker_cash


class TestFrozenRatios:
    def test_ratios_are_the_frozen_demo_baseline(self) -> None:
        assert Decimal("0.24") == PAYROLL_RATIO
        assert Decimal("0.14") == SUPPLIERS_RATIO
        assert Decimal("0.40") == OPERATING_RESERVE_RATIO
        assert Decimal("0.22") == INVESTABLE_SURPLUS_RATIO
        total = PAYROLL_RATIO + SUPPLIERS_RATIO + OPERATING_RESERVE_RATIO
        assert total + INVESTABLE_SURPLUS_RATIO == Decimal("1")


class TestInitializationAt100k:
    """Required proof 1: $100,000 initialization."""

    def test_seed_amounts_match_frozen_demo_values(self) -> None:
        seed = seed_scenario(Decimal("100000"), date(2026, 9, 1))
        payroll, suppliers = seed.obligations
        assert payroll.name == "payroll"
        assert payroll.amount == Decimal("24000.00")
        assert suppliers.name == "suppliers"
        assert suppliers.amount == Decimal("14000.00")
        assert seed.operating_reserve == Decimal("40000.00")
        assert seed.investable_surplus == Decimal("22000.00")
        assert seed.obligations_total == Decimal("38000.00")

    def test_obligations_use_explicit_iso_dates(self) -> None:
        seed = seed_scenario(Decimal("100000"), date(2026, 9, 1))
        payroll, suppliers = seed.obligations
        assert payroll.due_date == date(2026, 9, 11)
        assert suppliers.due_date == date(2026, 9, 19)

    def test_parts_sum_exactly_to_opening_cash(self) -> None:
        seed = seed_scenario(Decimal("100000"), date(2026, 9, 1))
        total = seed.obligations_total + seed.operating_reserve + seed.investable_surplus
        assert total == Decimal("100000")


class TestArbitraryOpeningCash:
    """Required proof 2: arbitrary opening cash produces the same ratios."""

    @pytest.mark.parametrize("cash", ["500000", "123456.78", "77777.77"])
    def test_seed_preserves_ratios(self, cash: str) -> None:
        opening = Decimal(cash)
        seed = seed_scenario(opening, date(2026, 9, 1))
        payroll, suppliers = seed.obligations
        assert payroll.amount == round_money(PAYROLL_RATIO * opening, ROUND_DOWN)
        assert suppliers.amount == round_money(SUPPLIERS_RATIO * opening, ROUND_DOWN)
        assert seed.operating_reserve == round_money(OPERATING_RESERVE_RATIO * opening, ROUND_DOWN)
        assert payroll.amount / opening <= PAYROLL_RATIO
        assert suppliers.amount / opening <= SUPPLIERS_RATIO
        assert seed.operating_reserve / opening <= OPERATING_RESERVE_RATIO
        assert seed.investable_surplus >= round_money(
            INVESTABLE_SURPLUS_RATIO * opening, ROUND_DOWN
        )

    def test_seed_500k_matches_spec_illustrative_baseline(self) -> None:
        seed = seed_scenario(Decimal("500000"), date(2026, 9, 1))
        payroll, suppliers = seed.obligations
        assert payroll.amount == Decimal("120000.00")
        assert suppliers.amount == Decimal("70000.00")
        assert seed.operating_reserve == Decimal("200000.00")
        assert seed.investable_surplus == Decimal("110000.00")

    def test_seed_rejects_ratios_exceeding_cash(self) -> None:
        with pytest.raises(ValueError):
            seed_scenario(
                Decimal("1000"),
                date(2026, 9, 1),
                payroll_ratio=Decimal("0.8"),
                suppliers_ratio=Decimal("0.3"),
                reserve_ratio=Decimal("0.1"),
            )


class TestNoRescalingAfterCashMovement:
    """Required proof 3: later cash movements do NOT rescale seeded
    obligations. The seed is immutable and the liquidity engine consumes it
    verbatim; no rescale code path exists."""

    def test_cash_loss_does_not_rescale_obligations_or_reserve(self) -> None:
        seed = seed_scenario(Decimal("100000"), date(2026, 9, 1))
        moved_broker = BrokerCashState(
            cash=Decimal("90000"),
            buying_power=Decimal("360000"),
            non_marginable_buying_power=Decimal("90000"),
            multiplier=Decimal("4"),
            as_of=DEFAULT_NOW,
        )
        projection = compute_liquidity(
            broker=moved_broker,
            obligations=seed.obligations,
            settlement_events=(),
            operating_reserve=seed.operating_reserve,
            as_of=DEFAULT_NOW.date(),
        )
        payroll, suppliers = projection.obligations
        assert payroll.amount == Decimal("24000.00")
        assert suppliers.amount == Decimal("14000.00")
        assert projection.operating_reserve == Decimal("40000.00")
        assert projection.protected_liquidity == Decimal("78000.00")
        assert projection.investable_cash == Decimal("12000.00")

    def test_seed_is_immutable(self) -> None:
        import dataclasses

        seed = seed_scenario(Decimal("100000"), date(2026, 9, 1))
        with pytest.raises(dataclasses.FrozenInstanceError):
            seed.operating_reserve = Decimal("1")  # type: ignore[misc]

    def test_engine_inputs_broker_environment_is_typed(self) -> None:
        execution = ExecutionContext(
            environment=BrokerEnvironment.PAPER,
            environment_verified=True,
            kill_switch_active=False,
            now=DEFAULT_NOW,
        )
        assert execution.environment is BrokerEnvironment.PAPER
        broker = phase1_broker_cash()
        assert broker.cash == Decimal("100000")
        assert broker.multiplier == Decimal("4")
        assert broker.cash > ZERO
