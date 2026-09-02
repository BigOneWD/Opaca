"""RED-phase contracts for deterministic Wheel option order identity."""

from __future__ import annotations

from decimal import Decimal

import pytest
from opaca.wheel.models import WheelAction
from opaca.wheel.order_id import is_valid_wheel_client_order_id, wheel_client_order_id


def order_id(**overrides: object) -> str:
    values: dict[str, object] = {
        "wheel_decision_run_id": "run-2026-09-03-001",
        "attempt_number": 1,
        "occ_symbol": "SPY260903P00746000",
        "action": WheelAction.SELL_CASH_SECURED_PUT,
        "contracts": 1,
        "limit_premium": Decimal("1.00"),
    }
    values.update(overrides)
    return wheel_client_order_id(**values)  # type: ignore[arg-type]


def test_same_logical_order_has_same_client_id_and_valid_shape() -> None:
    first = order_id()

    assert order_id() == first
    assert is_valid_wheel_client_order_id(first)
    assert first.startswith("wheel-")
    assert len(first) <= 128


@pytest.mark.parametrize(
    "field,value",
    [
        ("wheel_decision_run_id", "run-2026-09-03-002"),
        ("attempt_number", 2),
        ("occ_symbol", "QQQ260903P00600000"),
        ("action", WheelAction.HOLD),
        ("contracts", 2),
        ("limit_premium", Decimal("1.01")),
    ],
)
def test_any_logical_order_field_change_changes_client_id(field: str, value: object) -> None:
    assert order_id(**{field: value}) != order_id()

