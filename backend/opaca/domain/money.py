"""Exact-decimal money arithmetic.

Every financial quantity in the Opaca domain layer (cash, notional, prices,
obligations, thresholds, liquidity headroom) is a ``decimal.Decimal``. Binary
floats are rejected at the boundary.

Rounding policy (explicit, per SPEC s8 "rounding must never increase the
intended budget"):

* ``round_money``   - quantize to whole cents, ROUND_HALF_UP by default. Used
                      for accounting values (proceeds, market values).
* ``round_budget``  - quantize to whole cents, ROUND_DOWN. Used whenever a
                      budget/notional is derived so rounding can never
                      increase the intended deployment.
* ``round_quantity``- quantize to 1e-9 shares, ROUND_DOWN, matching the
                      fractional precision observed in Phase -1B evidence.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
SHARE_INCREMENT = Decimal("0.000000001")
ZERO = Decimal("0")


class MoneyError(ValueError):
    """Raised for invalid or non-exact monetary input."""


def money(value: str | int | Decimal) -> Decimal:
    """Coerce to a validated finite Decimal. Floats are forbidden."""
    if isinstance(value, float):
        raise MoneyError("binary float is forbidden for financial amounts")
    if isinstance(value, bool):
        raise MoneyError("bool is not a financial amount")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MoneyError(f"not a decimal amount: {value!r}") from exc
    if not result.is_finite():
        raise MoneyError(f"non-finite amount: {value!r}")
    return result


def non_negative_money(value: str | int | Decimal) -> Decimal:
    result = money(value)
    if result < 0:
        raise MoneyError(f"amount must be >= 0, got {result}")
    return result


def positive_money(value: str | int | Decimal) -> Decimal:
    result = money(value)
    if result <= 0:
        raise MoneyError(f"amount must be > 0, got {result}")
    return result


def round_money(value: str | int | Decimal, rounding: str = ROUND_HALF_UP) -> Decimal:
    """Quantize to cents with an explicit rounding mode (default HALF_UP)."""
    return money(value).quantize(CENT, rounding=rounding)


def round_budget(value: str | int | Decimal) -> Decimal:
    """Quantize a budget/notional DOWN to cents (never increases the budget)."""
    return money(value).quantize(CENT, rounding=ROUND_DOWN)


def round_quantity(value: str | int | Decimal) -> Decimal:
    """Quantize a share quantity down to the 1e-9 share increment."""
    result = money(value).quantize(SHARE_INCREMENT, rounding=ROUND_DOWN)
    if result <= 0:
        raise MoneyError(f"quantity must be > 0 after rounding, got {value!r}")
    return result
