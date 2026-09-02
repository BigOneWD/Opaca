"""Deterministic post-fill and expiry reconciliation for Wheel CSPs."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from opaca.domain.money import money, non_negative_money, positive_money
from opaca.wheel.models import OptionPosition, OptionPositionSide, OptionRight, WheelState
from opaca.wheel.store import WheelAccountMismatchError, WheelPersistenceError, WheelStore

__all__ = [
    "WheelAssignmentEvidence",
    "WheelBrokerOrder",
    "WheelExpiryEvidence",
    "WheelReconciliationResult",
    "WheelReconciliationStatus",
    "WheelState",
    "assignment_cash_tolerance",
    "reconcile_wheel",
]

ASSIGNMENT_CASH_TOLERANCE_FLOOR = Decimal("5.00")
ASSIGNMENT_CASH_TOLERANCE_RATE = Decimal("0.0005")
EXPIRATION_RECONCILIATION_TIME = time(9, 35)
_EASTERN = ZoneInfo("America/New_York")


class WheelReconciliationStatus(StrEnum):
    RECONCILED = "RECONCILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class WheelReconciliationResult:
    status: WheelReconciliationStatus
    wheel_state: WheelState
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))


@dataclass(frozen=True)
class WheelBrokerOrder:
    """Sanitized authoritative order read-back supplied by the read gateway."""

    client_order_id: str
    occ_symbol: str
    side: str
    right: OptionRight
    status: str
    contracts: int
    filled_contracts: int

    def __post_init__(self) -> None:
        if not self.client_order_id.strip() or not self.occ_symbol.strip():
            raise ValueError("broker order identity must be non-empty")
        if not isinstance(self.right, OptionRight):
            raise ValueError("broker order right must be an OptionRight")
        if isinstance(self.contracts, bool) or self.contracts < 1:
            raise ValueError("broker order contracts must be positive")
        if (
            isinstance(self.filled_contracts, bool)
            or not 0 <= self.filled_contracts <= self.contracts
        ):
            raise ValueError("broker order filled_contracts is invalid")
        object.__setattr__(self, "side", self.side.strip().upper())
        object.__setattr__(self, "status", self.status.strip().upper())


def _non_negative_decimal(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError(f"{name} must be a Decimal")
    return non_negative_money(value)


@dataclass(frozen=True)
class WheelAssignmentEvidence:
    before_shares: Decimal
    after_shares: Decimal
    cash_delta: Decimal
    after_market_value: Decimal
    shares_attributable: bool = True
    contradictory_activity: bool = False

    def __post_init__(self) -> None:
        for name in ("before_shares", "after_shares", "after_market_value"):
            object.__setattr__(
                self,
                name,
                _non_negative_decimal(getattr(self, name), name),
            )
        object.__setattr__(self, "cash_delta", money(self.cash_delta))


@dataclass(frozen=True)
class WheelExpiryEvidence:
    before_shares: Decimal
    after_shares: Decimal
    cash_delta: Decimal
    assignment_evidence_present: bool = False
    contradictory_activity: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "before_shares",
            _non_negative_decimal(self.before_shares, "before_shares"),
        )
        object.__setattr__(
            self,
            "after_shares",
            _non_negative_decimal(self.after_shares, "after_shares"),
        )
        object.__setattr__(self, "cash_delta", money(self.cash_delta))


def assignment_cash_tolerance(assignment_capital: Decimal) -> Decimal:
    """Return max($5.00, 0.0005 × assignment capital), cent-exact."""
    capital = positive_money(assignment_capital)
    return max(
        ASSIGNMENT_CASH_TOLERANCE_FLOOR,
        ASSIGNMENT_CASH_TOLERANCE_RATE * capital,
    ).quantize(Decimal("0.01"))


def _unknown(
    store: WheelStore,
    underlying: str | None,
    *reasons: str,
) -> WheelReconciliationResult:
    if underlying is not None:
        with suppress(WheelPersistenceError, ValueError, TypeError):
            store.persist_wheel_state(underlying, WheelState.UNKNOWN)
    return WheelReconciliationResult(
        status=WheelReconciliationStatus.UNKNOWN,
        wheel_state=WheelState.UNKNOWN,
        reasons=reasons or ("reconciliation facts are incomplete or contradictory",),
    )


def _expiry_threshold(expiration: date, next_regular_session: Callable[[date], date]) -> datetime:
    next_session = next_regular_session(expiration)
    if next_session <= expiration:
        raise ValueError("next regular session must follow expiration")
    return datetime.combine(next_session, EXPIRATION_RECONCILIATION_TIME, tzinfo=_EASTERN)


def reconcile_wheel(
    store: WheelStore,
    *,
    account_id: str,
    client_order_id: str,
    expected_occ_symbol: str,
    expected_contracts: int,
    expected_assignment_capital: Decimal,
    expected_multiplier: Decimal,
    broker_order: WheelBrokerOrder | None,
    option_position: OptionPosition | None,
    assignment: WheelAssignmentEvidence | None = None,
    expiration: date | None = None,
    next_regular_session: Callable[[date], date] | None = None,
    expiry: WheelExpiryEvidence | None = None,
    now: datetime,
) -> WheelReconciliationResult:
    """Reconcile one exact local logical order against supplied read models."""
    if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(None):
        raise ValueError("now must be timezone-aware UTC")
    if assignment is not None and expiry is not None:
        return _unknown(store, None, "assignment and expiry hypotheses conflict")
    try:
        capital = positive_money(expected_assignment_capital)
        multiplier = positive_money(expected_multiplier)
    except (TypeError, ValueError):
        return _unknown(store, None, "assignment arithmetic is invalid")
    if (
        isinstance(expected_contracts, bool)
        or expected_contracts < 1
        or not client_order_id.strip()
        or not expected_occ_symbol.strip()
    ):
        return _unknown(store, None, "expected Wheel order identity is invalid")

    local = store._conn.execute(
        "SELECT client_order_id, occ_symbol, status, reservation_id, assignment_capital "
        "FROM wheel_orders WHERE client_order_id = ?",
        (client_order_id,),
    ).fetchone()
    if local is None:
        underlying = option_position.underlying if option_position is not None else None
        return _unknown(store, underlying, "LOCAL_AUTHORIZED_ORDER_MISSING")

    reservation_id = local["reservation_id"]
    reservation = None
    if reservation_id is not None:
        reservation = store._conn.execute(
            "SELECT underlying, amount, status, kind FROM wheel_reservations "
            "WHERE reservation_id = ?",
            (str(reservation_id),),
        ).fetchone()
    underlying = str(reservation["underlying"]) if reservation is not None else None
    try:
        store.assert_account_binding(account_id)
    except WheelAccountMismatchError:
        return _unknown(store, underlying, "COMPETITION_ACCOUNT_BINDING_MISMATCH")
    except WheelPersistenceError:
        return _unknown(store, underlying, "COMPETITION_ACCOUNT_BINDING_UNAVAILABLE")

    if str(local["client_order_id"]) != client_order_id:
        return _unknown(store, underlying, "LOCAL_CLIENT_ORDER_ID_MISMATCH")
    if str(local["occ_symbol"]) != expected_occ_symbol:
        return _unknown(store, underlying, "LOCAL_OCC_SYMBOL_MISMATCH")
    if str(local["status"]) == "UNKNOWN":
        return _unknown(store, underlying, "LOCAL_ORDER_UNKNOWN")
    if local["assignment_capital"] is None:
        return _unknown(store, underlying, "LOCAL_ASSIGNMENT_CAPITAL_MISSING")
    try:
        local_capital = money(str(local["assignment_capital"]))
    except ValueError:
        return _unknown(store, underlying, "LOCAL_ASSIGNMENT_CAPITAL_INVALID")
    if local_capital != capital:
        return _unknown(store, underlying, "LOCAL_ASSIGNMENT_CAPITAL_MISMATCH")
    if reservation is None or str(reservation["status"]) != "ACTIVE":
        return _unknown(store, underlying, "ACTIVE_ASSIGNMENT_RESERVATION_MISSING")
    if str(reservation["kind"]) != "CASH_DEPLOYMENT":
        return _unknown(store, underlying, "ASSIGNMENT_RESERVATION_KIND_MISMATCH")
    try:
        reservation_amount = money(str(reservation["amount"]))
    except ValueError:
        return _unknown(store, underlying, "ASSIGNMENT_RESERVATION_AMOUNT_INVALID")
    if reservation_amount != capital:
        return _unknown(store, underlying, "ASSIGNMENT_RESERVATION_AMOUNT_MISMATCH")
    if broker_order is None:
        return _unknown(store, underlying, "BROKER_ORDER_READBACK_MISSING")
    if broker_order.client_order_id != client_order_id:
        return _unknown(store, underlying, "BROKER_CLIENT_ORDER_ID_MISMATCH")
    if broker_order.occ_symbol != expected_occ_symbol:
        return _unknown(store, underlying, "BROKER_OCC_SYMBOL_MISMATCH")
    if broker_order.side != "SELL" or broker_order.right is not OptionRight.PUT:
        return _unknown(store, underlying, "BROKER_ORDER_IS_NOT_OPENING_SHORT_PUT")

    if assignment is not None:
        if (
            option_position is not None
            or broker_order.status != "FILLED"
            or broker_order.contracts != expected_contracts
            or broker_order.filled_contracts != expected_contracts
        ):
            return _unknown(store, underlying, "ASSIGNMENT_OPTION_OR_ORDER_EVIDENCE_CONTRADICTORY")
        expected_shares = multiplier * expected_contracts
        if assignment.after_shares - assignment.before_shares != expected_shares:
            return _unknown(store, underlying, "ASSIGNMENT_SHARE_DELTA_MISMATCH")
        if not assignment.shares_attributable or assignment.contradictory_activity:
            return _unknown(store, underlying, "ASSIGNMENT_SHARES_UNEXPLAINED")
        expected_cash_delta = -capital
        if abs(assignment.cash_delta - expected_cash_delta) > assignment_cash_tolerance(capital):
            return _unknown(store, underlying, "ASSIGNMENT_CASH_DELTA_OUTSIDE_TOLERANCE")
        if underlying is None or expected_shares != expected_shares.to_integral_value():
            return _unknown(store, underlying, "ASSIGNMENT_MULTIPLIER_QUANTITY_INVALID")
        try:
            store.convert_assignment_to_share_lot(
                reservation_id=str(reservation_id),
                lot_id=f"assignment:{client_order_id}",
                underlying=underlying,
                shares=int(expected_shares),
                assignment_basis=capital / expected_shares,
                market_value=assignment.after_market_value,
                now=now,
                wheel_state=WheelState.SHARES_HELD,
            )
        except (WheelPersistenceError, ValueError, TypeError):
            return _unknown(store, underlying, "ASSIGNMENT_TRANSITION_FAILED")
        return WheelReconciliationResult(
            status=WheelReconciliationStatus.RECONCILED,
            wheel_state=WheelState.SHARES_HELD,
            reasons=("assignment facts agree; attributable shares persisted before release",),
        )

    if expiry is not None:
        if expiration is None or next_regular_session is None:
            return _unknown(store, underlying, "EXPIRY_SESSION_DATA_MISSING")
        try:
            threshold = _expiry_threshold(expiration, next_regular_session)
        except ValueError:
            return _unknown(store, underlying, "EXPIRY_SESSION_DATA_INVALID")
        if now < threshold:
            return _unknown(store, underlying, "EXPIRY_RECONCILIATION_BUFFER_NOT_ELAPSED")
        terminal = {"EXPIRED", "CANCELLED", "CANCELED", "REJECTED", "NOT_SUBMITTED"}
        if (
            option_position is not None
            or broker_order.status not in terminal
            or broker_order.filled_contracts != 0
            or expiry.assignment_evidence_present
            or expiry.contradictory_activity
            or expiry.before_shares != expiry.after_shares
            or expiry.cash_delta != 0
        ):
            return _unknown(store, underlying, "EXPIRY_EVIDENCE_INCOMPLETE_OR_CONTRADICTORY")
        try:
            released = store.release_assignment_if_proven_no_exposure(
                str(reservation_id),
                proven=True,
                now=now,
                wheel_state=WheelState.CASH,
            )
        except (WheelPersistenceError, ValueError, TypeError):
            released = False
        if not released:
            return _unknown(store, underlying, "EXPIRY_RESERVATION_RELEASE_FAILED")
        return WheelReconciliationResult(
            status=WheelReconciliationStatus.RECONCILED,
            wheel_state=WheelState.CASH,
            reasons=("expiry-worthless facts agree after the reconciliation buffer",),
        )

    if option_position is None:
        return _unknown(store, underlying, "SHORT_PUT_POSITION_MISSING")
    if (
        broker_order.contracts != expected_contracts
        or broker_order.filled_contracts != expected_contracts
        or option_position.occ_symbol != expected_occ_symbol
        or option_position.underlying != underlying
        or option_position.right is not OptionRight.PUT
        or option_position.side is not OptionPositionSide.SHORT
        or option_position.contracts != expected_contracts
    ):
        return _unknown(store, underlying, "SHORT_PUT_POSITION_IDENTITY_OR_QUANTITY_MISMATCH")
    try:
        store.persist_wheel_state(underlying, WheelState.SHORT_PUT_OPEN)
    except (WheelPersistenceError, ValueError, TypeError):
        return _unknown(store, underlying, "SHORT_PUT_STATE_PERSISTENCE_FAILED")
    return WheelReconciliationResult(
        status=WheelReconciliationStatus.RECONCILED,
        wheel_state=WheelState.SHORT_PUT_OPEN,
        reasons=("local order, broker order, short PUT, reservation, and account agree",),
    )
