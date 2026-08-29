"""Money error boundary (red-team RT-05).

Public money validation/rounding boundaries return MoneyError (the
documented domain validation error), never a raw decimal.InvalidOperation.
Binary floats remain forbidden.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

import pytest
from opaca.domain.models import BrokerCashState, Position, ProposedOrder, Side
from opaca.domain.money import (
    MAGNITUDE_LIMIT,
    MoneyError,
    money,
    round_budget,
    round_money,
    round_quantity,
)
from opaca.treasury.scenario import seed_scenario

from tests.helpers import DEFAULT_NOW


class TestQuantizeBoundaryReturnsMoneyError:
    def test_26_significant_digits_still_quantize(self) -> None:
        assert round_budget(Decimal("9" * 26)) == Decimal("9" * 26)
        assert round_money(Decimal("9" * 26)) == Decimal("9" * 26)

    def test_27_significant_digits_raise_money_error_not_invalid_operation(self) -> None:
        assert not issubclass(InvalidOperation, ValueError)
        for boundary in (round_budget, round_money, round_quantity):
            with pytest.raises(MoneyError):
                boundary(Decimal("9" * 27))

    def test_30_and_larger_magnitudes_raise_money_error(self) -> None:
        for digits in (30, 40, 100):
            with pytest.raises(MoneyError):
                round_budget(Decimal("9" * digits))

    def test_money_rejects_magnitudes_beyond_the_boundary(self) -> None:
        assert Decimal("1e26") == MAGNITUDE_LIMIT
        with pytest.raises(MoneyError):
            money(Decimal("9" * 27))
        with pytest.raises(MoneyError):
            money(MAGNITUDE_LIMIT)
        assert money(MAGNITUDE_LIMIT - 1) == MAGNITUDE_LIMIT - 1


class TestModelBoundariesFailWithMoneyError:
    def test_very_large_broker_cash_state_is_rejected(self) -> None:
        with pytest.raises(MoneyError):
            BrokerCashState(
                cash=Decimal("9" * 30),
                buying_power=Decimal("0"),
                non_marginable_buying_power=Decimal("0"),
                multiplier=Decimal("1"),
                as_of=DEFAULT_NOW,
            )

    def test_very_large_position_values_are_rejected(self) -> None:
        with pytest.raises(MoneyError):
            Position("SGOV", Decimal("9" * 30), Decimal("9" * 30), Decimal("0"))

    def test_huge_quantity_is_rejected_cleanly(self) -> None:
        """A quantity at 1e19 shares passes the magnitude boundary but
        cannot be quantized to 1e-9 within the decimal precision; the
        rounding boundary converts the InvalidOperation to MoneyError."""
        with pytest.raises(MoneyError):
            round_quantity(Decimal("1" + "0" * 19))
        with pytest.raises(MoneyError):
            ProposedOrder(
                "big", 0, "SGOV", Side.BUY, Decimal("1" + "0" * 19), Decimal("100.69"), "opaca-x"
            )

    def test_seed_scenario_with_excessive_magnitude_raises_money_error(self) -> None:
        with pytest.raises(MoneyError):
            seed_scenario(Decimal("9" * 30), date(2026, 9, 1))


class TestNoFloatSupport:
    def test_float_is_forbidden_at_every_boundary(self) -> None:
        # Floats are deliberately passed to the runtime boundary; the type
        # system forbids them and the boundary must reject them too.
        with pytest.raises(MoneyError):
            money(1.5)  # type: ignore[arg-type]
        with pytest.raises(MoneyError):
            round_budget(1.5)  # type: ignore[arg-type]
        with pytest.raises(MoneyError):
            round_money(1.5)  # type: ignore[arg-type]
        with pytest.raises(MoneyError):
            round_quantity(1.5)  # type: ignore[arg-type]
