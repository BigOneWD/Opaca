"""Typed domain models for the deterministic treasury core.

No dictionaries with undocumented string keys: every controlled state is an
enum and every aggregate is a frozen dataclass with validated fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum

from opaca.domain.money import (
    ZERO,
    money,
    non_negative_money,
    positive_money,
    round_budget,
    round_quantity,
)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class BrokerEnvironment(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class AssetStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class DecisionKind(str, Enum):
    """Closed enum of agent decisions (SPEC s7). Modeled here for typing only;
    LLM proposal generation is out of scope for the treasury core."""

    ALLOCATE = "allocate"
    REBALANCE = "rebalance"
    LIQUIDATE = "liquidate"
    HOLD = "hold"


class AuthorityResult(str, Enum):
    AUTO = "AUTO"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    REJECT = "REJECT"


class CheckId(str, Enum):
    CHECK_00 = "CHECK-00"
    CHECK_01 = "CHECK-01"
    CHECK_02 = "CHECK-02"
    CHECK_03 = "CHECK-03"
    CHECK_04 = "CHECK-04"
    CHECK_05 = "CHECK-05"
    CHECK_06 = "CHECK-06"
    CHECK_07 = "CHECK-07"
    CHECK_08 = "CHECK-08"
    CHECK_09 = "CHECK-09"
    CHECK_10 = "CHECK-10"
    CHECK_11 = "CHECK-11"
    CHECK_12 = "CHECK-12"
    CHECK_13 = "CHECK-13"
    CHECK_14 = "CHECK-14"
    CHECK_15 = "CHECK-15"
    CHECK_16 = "CHECK-16"


class OrderState(str, Enum):
    """Internal order states (SPEC s13). The state machine itself is a later
    phase; the enum exists so CHECK-10 can classify unresolved orders."""

    PROPOSED = "PROPOSED"
    POLICY_VALIDATED = "POLICY_VALIDATED"
    AUTO_AUTHORIZED = "AUTO_AUTHORIZED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    SUBMITTED = "SUBMITTED"
    UNKNOWN = "UNKNOWN"
    UNKNOWN_REQUIRES_REVIEW = "UNKNOWN_REQUIRES_REVIEW"
    ACCEPTED = "ACCEPTED"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELED = "CANCELED"
    CANCELED_REMAINDER = "CANCELED_REMAINDER"
    RECONCILED = "RECONCILED"
    FAILED = "FAILED"


#: Fail-closed classification: any order that might still exist at the broker
#: (including UNKNOWN) is unresolved for opposing-order purposes.
UNRESOLVED_ORDER_STATES = frozenset(
    {
        OrderState.PROPOSED,
        OrderState.POLICY_VALIDATED,
        OrderState.AUTO_AUTHORIZED,
        OrderState.APPROVAL_REQUIRED,
        OrderState.HUMAN_APPROVED,
        OrderState.SUBMITTED,
        OrderState.UNKNOWN,
        OrderState.UNKNOWN_REQUIRES_REVIEW,
        OrderState.ACCEPTED,
        OrderState.NEW,
        OrderState.PARTIALLY_FILLED,
    }
)


def _require_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be timezone-aware UTC, got {value!r}")
    return value


@dataclass(frozen=True)
class BrokerCashState:
    """Reconciled Alpaca account cash snapshot (single ledger, SPEC s3).

    ``buying_power`` is carried for audit/display only. Treasury and policy
    code MUST NOT read it: Phase -1A observed buying_power = 4x cash, i.e.
    broker leverage, not corporate liquidity (CHECK-06 / CHECK-11).
    """

    cash: Decimal
    buying_power: Decimal
    non_marginable_buying_power: Decimal
    multiplier: Decimal
    as_of: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "cash", non_negative_money(self.cash))
        object.__setattr__(self, "buying_power", non_negative_money(self.buying_power))
        object.__setattr__(
            self,
            "non_marginable_buying_power",
            non_negative_money(self.non_marginable_buying_power),
        )
        object.__setattr__(self, "multiplier", non_negative_money(self.multiplier))
        object.__setattr__(self, "as_of", _require_utc(self.as_of, "as_of"))


@dataclass(frozen=True)
class Obligation:
    """A dated corporate liability. ``due_date`` is an explicit ISO date
    (SPEC s4); relative offsets exist only in scenario seeding."""

    obligation_id: str
    name: str
    amount: Decimal
    due_date: date

    def __post_init__(self) -> None:
        if not self.obligation_id:
            raise ValueError("obligation_id must be non-empty")
        object.__setattr__(self, "amount", positive_money(self.amount))


@dataclass(frozen=True)
class SettlementEvent:
    """Derived (not broker-reported) availability of sale proceeds (SPEC s5,
    Amendment B). Proceeds are NOT operationally available until
    ``settlement_date`` regardless of paper-account cash crediting."""

    event_id: str
    symbol: str
    trade_date: date
    settlement_date: date
    amount: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", non_negative_money(self.amount))
        if self.settlement_date < self.trade_date:
            raise ValueError("settlement_date cannot precede trade_date")


@dataclass(frozen=True)
class Position:
    """Reconciled long position. Opaca is long-only (CHECK-16)."""

    symbol: str
    quantity: Decimal
    quantity_available: Decimal
    market_value: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantity", non_negative_money(self.quantity))
        object.__setattr__(self, "quantity_available", non_negative_money(self.quantity_available))
        if self.quantity_available > self.quantity:
            raise ValueError("quantity_available cannot exceed quantity")
        object.__setattr__(self, "market_value", non_negative_money(self.market_value))


@dataclass(frozen=True)
class AssetState:
    """Alpaca tradability snapshot for one instrument (CHECK-03 / CHECK-05)."""

    symbol: str
    status: AssetStatus
    tradable: bool
    fractionable: bool

    @property
    def is_permitted_for_trading(self) -> bool:
        return self.status is AssetStatus.ACTIVE and self.tradable


@dataclass(frozen=True)
class LiquidityPolicy:
    operating_reserve: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "operating_reserve", non_negative_money(self.operating_reserve))


@dataclass(frozen=True)
class PrecloseBlackoutConfig:
    """CHECK-15: deterministic, configurable, may be disabled."""

    enabled: bool
    minutes_before_close: int

    def __post_init__(self) -> None:
        if self.minutes_before_close < 0:
            raise ValueError("minutes_before_close must be >= 0")


@dataclass(frozen=True)
class InvestmentPolicy:
    permitted_symbols: frozenset[str]
    concentration_max_fraction: Decimal
    min_trade_notional: Decimal
    preclose_blackout: PrecloseBlackoutConfig

    def __post_init__(self) -> None:
        if not self.permitted_symbols:
            raise ValueError("permitted_symbols must not be empty")
        fraction = non_negative_money(self.concentration_max_fraction)
        if fraction > Decimal("1"):
            raise ValueError("concentration_max_fraction must be <= 1")
        object.__setattr__(self, "concentration_max_fraction", fraction)
        object.__setattr__(self, "min_trade_notional", positive_money(self.min_trade_notional))


@dataclass(frozen=True)
class AuthorityPolicy:
    """Delegated-authority dimensions (SPEC s9 CHECK-07, CHECK-13)."""

    per_order_notional_max: Decimal
    per_proposal_notional_max: Decimal
    rolling_24h_notional_max: Decimal
    rolling_order_count_max: int
    runaway_hourly_order_count_max: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "per_order_notional_max", positive_money(self.per_order_notional_max)
        )
        object.__setattr__(
            self, "per_proposal_notional_max", positive_money(self.per_proposal_notional_max)
        )
        object.__setattr__(
            self, "rolling_24h_notional_max", positive_money(self.rolling_24h_notional_max)
        )
        if self.rolling_order_count_max < 1 or self.runaway_hourly_order_count_max < 1:
            raise ValueError("order-count limits must be >= 1")


@dataclass(frozen=True)
class ProposedAllocation:
    """LLM-shaped intent (SPEC s7). Modeled/validated only; proposal
    generation is out of scope for the treasury core."""

    symbol: str
    target_weight: Decimal
    horizon_bucket: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        weight = money(self.target_weight)
        object.__setattr__(self, "target_weight", weight)
        if not (ZERO < weight <= Decimal("1")):
            raise ValueError(f"target_weight must be in (0, 1], got {weight}")


@dataclass(frozen=True)
class ProposedOrder:
    """One deterministic order leg. Notional is exact decimal qty x price,
    rounded down to cents so rounding never increases the budget."""

    proposal_id: str
    leg_index: int
    symbol: str
    side: Side
    quantity: Decimal
    reference_price: Decimal
    client_order_id: str

    def __post_init__(self) -> None:
        if not self.proposal_id:
            raise ValueError("proposal_id must be non-empty")
        if self.leg_index < 0:
            raise ValueError("leg_index must be >= 0")
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        object.__setattr__(self, "quantity", round_quantity(self.quantity))
        object.__setattr__(self, "reference_price", positive_money(self.reference_price))

    @property
    def notional(self) -> Decimal:
        return round_budget(self.quantity * self.reference_price)


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    legs: tuple[ProposedOrder, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.proposal_id:
            raise ValueError("proposal_id must be non-empty")
        object.__setattr__(self, "legs", tuple(self.legs))

    @property
    def buy_legs(self) -> tuple[ProposedOrder, ...]:
        return tuple(leg for leg in self.legs if leg.side is Side.BUY)

    @property
    def sell_legs(self) -> tuple[ProposedOrder, ...]:
        return tuple(leg for leg in self.legs if leg.side is Side.SELL)

    @property
    def total_buy_notional(self) -> Decimal:
        return sum((leg.notional for leg in self.buy_legs), ZERO)

    @property
    def total_sell_notional(self) -> Decimal:
        return sum((leg.notional for leg in self.sell_legs), ZERO)

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(leg.symbol for leg in self.legs)


@dataclass(frozen=True)
class UnresolvedOrder:
    """An order from any prior proposal that may still exist at the broker."""

    proposal_id: str
    symbol: str
    side: Side
    client_order_id: str
    state: OrderState

    @property
    def is_unresolved(self) -> bool:
        return self.state in UNRESOLVED_ORDER_STATES


@dataclass(frozen=True)
class AutonomousExecution:
    """One autonomously executed order, used for rolling-window limits."""

    timestamp: datetime
    notional: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _require_utc(self.timestamp, "timestamp"))
        object.__setattr__(self, "notional", non_negative_money(self.notional))


@dataclass(frozen=True)
class ExecutionContext:
    """Runtime control inputs evaluated as policy data (fail closed)."""

    environment: BrokerEnvironment
    environment_verified: bool
    kill_switch_active: bool
    now: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "now", _require_utc(self.now, "now"))


@dataclass(frozen=True)
class PolicyCheckResult:
    check_id: CheckId
    passed: bool
    hard: bool
    detail: str


@dataclass(frozen=True)
class PolicyDecision:
    passed: bool
    results: tuple[PolicyCheckResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(self.results))

    @property
    def violations(self) -> tuple[PolicyCheckResult, ...]:
        return tuple(r for r in self.results if not r.passed and r.hard)

    def result_for(self, check_id: CheckId) -> PolicyCheckResult:
        for result in self.results:
            if result.check_id is check_id:
                return result
        raise KeyError(f"{check_id} was not evaluated")


@dataclass(frozen=True)
class AuthorityDecision:
    result: AuthorityResult
    reasons: tuple[str, ...]
    policy_decision: PolicyDecision

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))

    @property
    def can_be_approved_by_human(self) -> bool:
        """Human approval only ever applies to APPROVAL_REQUIRED; it can
        never override a hard policy REJECT (SPEC s10)."""
        return self.result is AuthorityResult.APPROVAL_REQUIRED
