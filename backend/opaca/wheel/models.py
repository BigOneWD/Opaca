"""Typed domain models for the V1 Competition Wheel boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from opaca.domain.money import non_negative_money, positive_money


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    return value


def _require_utc(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(None)
    ):
        raise ValueError(f"{name} must be timezone-aware UTC, got {value!r}")
    return value


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


def _positive_money(value: object, name: str) -> Decimal:
    return positive_money(_money_input(value, name))


def _non_negative_money(value: object, name: str) -> Decimal:
    return non_negative_money(_money_input(value, name))


class WheelAction(StrEnum):
    SELL_CASH_SECURED_PUT = "SELL_CASH_SECURED_PUT"
    HOLD = "HOLD"


class OptionRight(StrEnum):
    PUT = "PUT"


class WheelState(StrEnum):
    CASH = "CASH"
    SHORT_PUT_OPEN = "SHORT_PUT_OPEN"
    SHARES_HELD = "SHARES_HELD"
    COVERED_CALL_OPEN = "COVERED_CALL_OPEN"
    UNKNOWN = "UNKNOWN"


class OptionPositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True, init=False)
class OptionContract:
    occ_symbol: str
    underlying: str
    right: OptionRight
    strike: Decimal
    expiration: date
    multiplier: Decimal
    active: bool
    tradable: bool

    def __init__(
        self,
        occ_symbol: object,
        underlying: object,
        right: object,
        strike: object,
        expiration: object,
        multiplier: object,
        active: object,
        tradable: object,
    ) -> None:
        if not isinstance(right, OptionRight):
            raise ValueError(f"right must be an OptionRight, got {right!r}")
        if not isinstance(expiration, date):
            raise ValueError(f"expiration must be a date, got {expiration!r}")
        if not isinstance(active, bool) or not isinstance(tradable, bool):
            raise ValueError("active and tradable must be bool values")
        object.__setattr__(self, "occ_symbol", _require_text(occ_symbol, "occ_symbol"))
        object.__setattr__(self, "underlying", _require_text(underlying, "underlying"))
        object.__setattr__(self, "right", right)
        object.__setattr__(self, "strike", _positive_money(strike, "strike"))
        object.__setattr__(self, "expiration", expiration)
        object.__setattr__(self, "multiplier", _positive_money(multiplier, "multiplier"))
        object.__setattr__(self, "active", active)
        object.__setattr__(self, "tradable", tradable)


@dataclass(frozen=True, init=False)
class OptionQuote:
    bid: Decimal
    ask: Decimal
    as_of: datetime

    def __init__(self, bid: object, ask: object, as_of: object) -> None:
        object.__setattr__(self, "bid", _non_negative_money(bid, "bid"))
        object.__setattr__(self, "ask", _non_negative_money(ask, "ask"))
        object.__setattr__(self, "as_of", _require_utc(as_of, "as_of"))


@dataclass(frozen=True, init=False)
class OptionIntent:
    action: WheelAction
    underlying: str
    market_view: str
    thesis: str
    willing_to_own_at_or_below: Decimal
    dte_preference: int
    confidence: Decimal

    def __init__(
        self,
        action: object,
        underlying: object,
        market_view: object,
        thesis: object,
        willing_to_own_at_or_below: object,
        dte_preference: object,
        confidence: object,
    ) -> None:
        if not isinstance(action, WheelAction):
            raise ValueError(f"action must be a WheelAction, got {action!r}")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "underlying", _require_text(underlying, "underlying"))
        object.__setattr__(self, "market_view", _require_text(market_view, "market_view"))
        object.__setattr__(self, "thesis", _require_text(thesis, "thesis"))
        object.__setattr__(
            self,
            "willing_to_own_at_or_below",
            _positive_money(willing_to_own_at_or_below, "willing_to_own_at_or_below"),
        )
        object.__setattr__(
            self,
            "dte_preference",
            _require_non_negative_int(dte_preference, "dte_preference"),
        )
        confidence_value = _non_negative_money(confidence, "confidence")
        if confidence_value > Decimal("1"):
            raise ValueError(f"confidence must be within [0, 1], got {confidence_value}")
        object.__setattr__(self, "confidence", confidence_value)


@dataclass(frozen=True, init=False)
class OptionPosition:
    occ_symbol: str
    underlying: str
    right: OptionRight
    contracts: int
    side: OptionPositionSide

    def __init__(
        self,
        occ_symbol: object,
        underlying: object,
        right: object,
        contracts: object,
        side: object,
    ) -> None:
        if not isinstance(right, OptionRight):
            raise ValueError(f"right must be an OptionRight, got {right!r}")
        if not isinstance(side, str):
            raise ValueError(f"side must be LONG or SHORT, got {side!r}")
        try:
            side_value = OptionPositionSide(side)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"side must be LONG or SHORT, got {side!r}") from exc
        object.__setattr__(self, "occ_symbol", _require_text(occ_symbol, "occ_symbol"))
        object.__setattr__(self, "underlying", _require_text(underlying, "underlying"))
        object.__setattr__(self, "right", right)
        object.__setattr__(self, "contracts", _require_positive_int(contracts, "contracts"))
        object.__setattr__(self, "side", side_value)


@dataclass(frozen=True, init=False)
class WheelShareLot:
    underlying: str
    shares: int
    assignment_basis: Decimal
    market_value: Decimal

    def __init__(
        self,
        underlying: object,
        shares: object,
        assignment_basis: object,
        market_value: object,
    ) -> None:
        object.__setattr__(self, "underlying", _require_text(underlying, "underlying"))
        object.__setattr__(self, "shares", _require_positive_int(shares, "shares"))
        object.__setattr__(
            self,
            "assignment_basis",
            _non_negative_money(assignment_basis, "assignment_basis"),
        )
        object.__setattr__(self, "market_value", _non_negative_money(market_value, "market_value"))


@dataclass(frozen=True, init=False)
class WheelApprovalBinding:
    wheel_decision_run_id: str
    attempt_number: int
    occ_symbol: str
    action: WheelAction
    contracts: int
    assignment_capital: Decimal
    approved_sell_limit_premium: Decimal
    approved_at: datetime
    expires_at: datetime

    def __init__(
        self,
        wheel_decision_run_id: object,
        attempt_number: object,
        occ_symbol: object,
        action: object,
        contracts: object,
        assignment_capital: object,
        approved_sell_limit_premium: object,
        approved_at: object,
        expires_at: object,
    ) -> None:
        if not isinstance(action, WheelAction):
            raise ValueError(f"action must be a WheelAction, got {action!r}")
        object.__setattr__(
            self,
            "wheel_decision_run_id",
            _require_text(wheel_decision_run_id, "wheel_decision_run_id"),
        )
        object.__setattr__(
            self,
            "attempt_number",
            _require_positive_int(attempt_number, "attempt_number"),
        )
        object.__setattr__(self, "occ_symbol", _require_text(occ_symbol, "occ_symbol"))
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "contracts", _require_positive_int(contracts, "contracts"))
        object.__setattr__(
            self,
            "assignment_capital",
            _positive_money(assignment_capital, "assignment_capital"),
        )
        object.__setattr__(
            self,
            "approved_sell_limit_premium",
            _non_negative_money(approved_sell_limit_premium, "approved_sell_limit_premium"),
        )
        object.__setattr__(self, "approved_at", _require_utc(approved_at, "approved_at"))
        object.__setattr__(self, "expires_at", _require_utc(expires_at, "expires_at"))
