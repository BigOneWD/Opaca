"""Immutable V1 Competition Wheel policy configuration."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from opaca.domain.money import non_negative_money


def _require_non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _require_positive_int(value: object, name: str) -> int:
    result = _require_non_negative_int(value, name)
    if result == 0:
        raise ValueError(f"{name} must be a positive integer")
    return result


def _money_input(value: object, name: str) -> str | int | Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"{name} must be a decimal-compatible value")
    return value


def _require_non_negative_money(value: object, name: str) -> Decimal:
    return non_negative_money(_money_input(value, name))


def _require_fraction(value: object, name: str) -> Decimal:
    result = _require_non_negative_money(value, name)
    if result > Decimal("1"):
        raise ValueError(f"{name} must be within [0, 1], got {result}")
    return result


@dataclass(frozen=True, init=False)
class WheelPolicy:
    min_dte_days: int = 1
    max_dte_days: int = 7
    max_quote_age_seconds: int = 15
    preclose_blackout_minutes: int = 30
    min_premium_yield_on_assignment: Decimal = Decimal("0.001")
    hard_per_underlying_fraction: Decimal = Decimal("0.25")
    auto_proposal_fraction: Decimal = Decimal("0.10")
    auto_underlying_fraction: Decimal = Decimal("0.10")
    auto_aggregate_fraction: Decimal = Decimal("0.20")
    approval_ttl_minutes: int = 5
    opening_contracts: int = 1
    max_agent_attempts_per_run: int = 2

    def __init__(
        self,
        min_dte_days: object = 1,
        max_dte_days: object = 7,
        max_quote_age_seconds: object = 15,
        preclose_blackout_minutes: object = 30,
        min_premium_yield_on_assignment: object = Decimal("0.001"),
        hard_per_underlying_fraction: object = Decimal("0.25"),
        auto_proposal_fraction: object = Decimal("0.10"),
        auto_underlying_fraction: object = Decimal("0.10"),
        auto_aggregate_fraction: object = Decimal("0.20"),
        approval_ttl_minutes: object = 5,
        opening_contracts: object = 1,
        max_agent_attempts_per_run: object = 2,
    ) -> None:
        min_dte = _require_non_negative_int(min_dte_days, "min_dte_days")
        if min_dte < 1:
            raise ValueError("min_dte_days must be >= 1")
        max_dte = _require_non_negative_int(max_dte_days, "max_dte_days")
        if max_dte < min_dte:
            raise ValueError("max_dte_days must be >= min_dte_days")
        object.__setattr__(self, "min_dte_days", min_dte)
        object.__setattr__(self, "max_dte_days", max_dte)
        object.__setattr__(
            self,
            "max_quote_age_seconds",
            _require_non_negative_int(max_quote_age_seconds, "max_quote_age_seconds"),
        )
        object.__setattr__(
            self,
            "preclose_blackout_minutes",
            _require_non_negative_int(preclose_blackout_minutes, "preclose_blackout_minutes"),
        )
        object.__setattr__(
            self,
            "min_premium_yield_on_assignment",
            _require_non_negative_money(
                min_premium_yield_on_assignment,
                "min_premium_yield_on_assignment",
            ),
        )
        for name, value in (
            ("hard_per_underlying_fraction", hard_per_underlying_fraction),
            ("auto_proposal_fraction", auto_proposal_fraction),
            ("auto_underlying_fraction", auto_underlying_fraction),
            ("auto_aggregate_fraction", auto_aggregate_fraction),
        ):
            object.__setattr__(self, name, _require_fraction(value, name))
        object.__setattr__(
            self,
            "approval_ttl_minutes",
            _require_positive_int(approval_ttl_minutes, "approval_ttl_minutes"),
        )
        opening = _require_positive_int(opening_contracts, "opening_contracts")
        if opening != 1:
            raise ValueError("opening_contracts must equal 1 for V1")
        object.__setattr__(self, "opening_contracts", opening)
        attempts = _require_positive_int(max_agent_attempts_per_run, "max_agent_attempts_per_run")
        if attempts != 2:
            raise ValueError("max_agent_attempts_per_run must equal 2 for V1")
        object.__setattr__(self, "max_agent_attempts_per_run", attempts)
