"""Translate Alpaca payloads into Treasury Core domain models. Fail closed."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal

from opaca.broker.errors import InvalidBrokerStateError
from opaca.calendar.us_trading_calendar import TradingSession
from opaca.domain.models import (
    AssetState,
    AssetStatus,
    BrokerCashState,
    OrderState,
    Position,
    Side,
    UnresolvedOrder,
)
from opaca.domain.money import MoneyError, money, non_negative_money
from opaca.persistence.types import OrderSnapshotRecord

ACCOUNT_REDACT_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "account_number",
        "api_key",
        "secret",
        "secret_key",
        "key_id",
        "token",
    }
)

#: Explicit Alpaca-status → internal-state mapping (SPEC s13). Observed in
#: Phase −1B: new, filled, canceled. Documented but unobserved statuses are
#: mapped conservatively. Anything else fails closed to UNKNOWN.
ALPACA_ORDER_STATUS_MAP: dict[str, OrderState] = {
    "new": OrderState.NEW,
    "pending_new": OrderState.NEW,
    "accepted": OrderState.ACCEPTED,
    "partially_filled": OrderState.PARTIALLY_FILLED,
    "filled": OrderState.FILLED,
    "canceled": OrderState.CANCELED,
    "cancelled": OrderState.CANCELED,
    "expired": OrderState.EXPIRED,
    "rejected": OrderState.REJECTED,
    "done_for_day": OrderState.UNKNOWN,
    "held": OrderState.UNKNOWN,
    "pending_cancel": OrderState.UNKNOWN,
    "pending_replace": OrderState.UNKNOWN,
    "stopped": OrderState.UNKNOWN,
    "suspended": OrderState.UNKNOWN,
    "calculated": OrderState.UNKNOWN,
}

UNRESOLVED_ALPACA_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.NEW,
        OrderState.ACCEPTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.UNKNOWN,
        OrderState.UNKNOWN_REQUIRES_REVIEW,
        OrderState.SUBMITTED,
    }
)


def as_mapping(raw: object) -> Mapping[str, object]:
    if isinstance(raw, Mapping):
        return raw
    dump = getattr(raw, "model_dump", None)
    if callable(dump):
        dumped = dump()
        if isinstance(dumped, Mapping):
            return dumped
    raise InvalidBrokerStateError(f"broker payload is not a mapping: {type(raw).__name__}")


def _require_field(data: Mapping[str, object], key: str) -> object:
    if key not in data or data[key] is None:
        raise InvalidBrokerStateError(f"missing broker field {key!r}")
    return data[key]


def _normalize_broker_enum_token(value: object) -> str | None:
    """Return a case-normalized token from a string or enum-like object.

    Prefers ``.value`` when it is a non-empty string. Otherwise accepts a plain
    string. Uses ``.name`` only when the value path is unavailable. Does not
    parse ``str(value)``, so the string ``"AssetStatus.ACTIVE"`` is not active.
    """
    raw = getattr(value, "value", None)
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token:
            return token
    if isinstance(value, str):
        token = value.strip().lower()
        return token or None
    name = getattr(value, "name", None)
    if isinstance(name, str):
        token = name.strip().lower()
        if token:
            return token
    return None


def parse_decimal_field(
    data: Mapping[str, object], key: str, *, allow_negative: bool = False
) -> Decimal:
    value = _require_field(data, key)
    if isinstance(value, float):
        raise InvalidBrokerStateError(f"broker field {key!r} is a binary float; fail closed")
    if isinstance(value, bool):
        raise InvalidBrokerStateError(f"broker field {key!r} is a bool; fail closed")
    if not isinstance(value, str | int | Decimal):
        raise InvalidBrokerStateError(f"broker field {key!r} is not a valid decimal")
    try:
        parsed = money(value) if allow_negative else non_negative_money(value)
    except (MoneyError, TypeError, ValueError) as exc:
        raise InvalidBrokerStateError(f"broker field {key!r} is not a valid decimal") from exc
    return parsed


def parse_optional_decimal(data: Mapping[str, object], key: str) -> Decimal | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if isinstance(value, float):
        raise InvalidBrokerStateError(f"broker field {key!r} is a binary float; fail closed")
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise InvalidBrokerStateError(f"broker field {key!r} is not a valid decimal")
    try:
        return non_negative_money(value)
    except (MoneyError, TypeError, ValueError) as exc:
        raise InvalidBrokerStateError(f"broker field {key!r} is not a valid decimal") from exc


def parse_datetime_field(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidBrokerStateError(f"{name} is not a timezone-aware timestamp") from exc
    if parsed.tzinfo is None:
        raise InvalidBrokerStateError(f"{name} is naive; fail closed")
    return parsed.astimezone(UTC)


def sanitize_account_diagnostics(data: Mapping[str, object]) -> dict[str, object]:
    """Preserve buying-power fields as diagnostics only. Never persist secrets."""
    keep = (
        "regt_buying_power",
        "daytrading_buying_power",
        "multiplier",
        "shorting_enabled",
        "status",
        "currency",
        "equity",
        "portfolio_value",
        "long_market_value",
        "short_market_value",
        "pattern_day_trader",
        "trading_blocked",
        "trade_suspended_by_user",
        "crypto_status",
        "options_approved_level",
        "options_trading_level",
    )
    out: dict[str, object] = {}
    for key in keep:
        if key in data:
            out[key] = data[key]
    return out


def adapt_account(raw: object, as_of: datetime) -> BrokerCashState:
    data = as_mapping(raw)
    try:
        return BrokerCashState(
            cash=parse_decimal_field(data, "cash"),
            buying_power=parse_decimal_field(data, "buying_power"),
            non_marginable_buying_power=parse_decimal_field(data, "non_marginable_buying_power"),
            multiplier=parse_decimal_field(data, "multiplier"),
            as_of=as_of if as_of.tzinfo is not None else parse_datetime_field(as_of, "as_of"),
        )
    except (MoneyError, ValueError) as exc:
        raise InvalidBrokerStateError("malformed account cash state") from exc


def adapt_position(raw: object) -> Position:
    data = as_mapping(raw)
    symbol = str(_require_field(data, "symbol"))
    if not symbol:
        raise InvalidBrokerStateError("position symbol is empty")
    side = data.get("side")
    side_token = _normalize_broker_enum_token(side) if side is not None else None
    if side is not None and side_token != "long":
        raise InvalidBrokerStateError(f"short/non-long position {symbol} is invalid for Opaca")
    qty_key = "qty" if "qty" in data else "quantity"
    available_key = "qty_available" if "qty_available" in data else "quantity_available"
    try:
        return Position(
            symbol=symbol,
            quantity=parse_decimal_field(data, qty_key),
            quantity_available=parse_decimal_field(data, available_key),
            market_value=parse_decimal_field(data, "market_value"),
        )
    except (MoneyError, ValueError) as exc:
        raise InvalidBrokerStateError(f"malformed position {symbol}") from exc


def adapt_asset(raw: object) -> AssetState:
    data = as_mapping(raw)
    symbol = str(_require_field(data, "symbol"))
    status_raw = _normalize_broker_enum_token(_require_field(data, "status"))
    if status_raw is None:
        raise InvalidBrokerStateError(f"unknown asset status for {symbol}")
    try:
        status = AssetStatus(status_raw)
    except ValueError as exc:
        raise InvalidBrokerStateError(f"unknown asset status {status_raw!r} for {symbol}") from exc
    tradable = data.get("tradable")
    fractionable = data.get("fractionable")
    if not isinstance(tradable, bool) or not isinstance(fractionable, bool):
        raise InvalidBrokerStateError(f"asset {symbol} tradable/fractionable must be bool")
    return AssetState(
        symbol=symbol,
        status=status,
        tradable=tradable,
        fractionable=fractionable,
    )


def map_order_status(status: str) -> OrderState:
    mapped = ALPACA_ORDER_STATUS_MAP.get(status.lower())
    if mapped is None:
        return OrderState.UNKNOWN
    return mapped


def adapt_order_snapshot(raw: object) -> OrderSnapshotRecord:
    data = as_mapping(raw)
    client_order_id = str(_require_field(data, "client_order_id"))
    if not client_order_id:
        raise InvalidBrokerStateError("order missing client_order_id")
    symbol = str(_require_field(data, "symbol"))
    side_token = _normalize_broker_enum_token(_require_field(data, "side"))
    if side_token is None:
        raise InvalidBrokerStateError("malformed order side")
    try:
        side = Side(side_token.upper())
    except ValueError as exc:
        raise InvalidBrokerStateError(f"malformed order side {side_token!r}") from exc
    status = _normalize_broker_enum_token(_require_field(data, "status"))
    if status is None:
        raise InvalidBrokerStateError("malformed order status")
    mapped = map_order_status(status)
    qty = parse_optional_decimal(data, "qty")
    if qty is None:
        qty = parse_optional_decimal(data, "quantity")
    filled = parse_optional_decimal(data, "filled_qty")
    if filled is None:
        filled = parse_optional_decimal(data, "filled_quantity")
    if qty is not None and filled is not None and filled > qty:
        raise InvalidBrokerStateError(f"order {client_order_id} filled_quantity exceeds quantity")
    broker_id = data.get("id")
    return OrderSnapshotRecord(
        client_order_id=client_order_id,
        broker_order_id=None if broker_id is None else str(broker_id),
        symbol=symbol,
        side=side.value,
        alpaca_status=status,
        mapped_state=mapped.value,
        quantity=qty,
        filled_quantity=filled,
    )


def validate_adapted_broker_rows(
    positions: Sequence[Position],
    orders: Sequence[OrderSnapshotRecord],
) -> None:
    """Fail closed on duplicate or internally contradictory broker rows."""
    seen_symbols: set[str] = set()
    for position in positions:
        if position.symbol in seen_symbols:
            raise InvalidBrokerStateError(f"duplicate broker position symbol {position.symbol}")
        seen_symbols.add(position.symbol)
    seen_client_ids: set[str] = set()
    seen_broker_ids: set[str] = set()
    for order in orders:
        if order.client_order_id in seen_client_ids:
            raise InvalidBrokerStateError(
                f"duplicate broker client_order_id {order.client_order_id}"
            )
        seen_client_ids.add(order.client_order_id)
        if order.broker_order_id:
            if order.broker_order_id in seen_broker_ids:
                raise InvalidBrokerStateError(f"duplicate broker order id {order.broker_order_id}")
            seen_broker_ids.add(order.broker_order_id)
        if (
            order.quantity is not None
            and order.filled_quantity is not None
            and order.filled_quantity > order.quantity
        ):
            raise InvalidBrokerStateError(
                f"order {order.client_order_id} filled_quantity exceeds quantity"
            )


def adapt_unresolved_order(record: OrderSnapshotRecord, *, proposal_id: str) -> UnresolvedOrder:
    try:
        side = Side(record.side)
        state = OrderState(record.mapped_state)
    except ValueError as exc:
        raise InvalidBrokerStateError("malformed unresolved order") from exc
    try:
        return UnresolvedOrder(
            proposal_id=proposal_id,
            symbol=record.symbol,
            side=side,
            client_order_id=record.client_order_id,
            state=state,
            quantity=record.quantity,
            filled_quantity=record.filled_quantity,
        )
    except (MoneyError, ValueError) as exc:
        raise InvalidBrokerStateError("malformed unresolved order quantities") from exc


def adapt_calendar_session(raw: object) -> TradingSession:
    data = as_mapping(raw)
    session_date = date.fromisoformat(str(_require_field(data, "date")))
    open_raw = _require_field(data, "open")
    close_raw = _require_field(data, "close")
    return TradingSession(
        session_date=session_date,
        open_time=_parse_clock_time(open_raw),
        close_time=_parse_clock_time(close_raw),
    )


def _parse_clock_time(value: object) -> time:
    if isinstance(value, time):
        return value
    text = str(value)
    if "T" in text:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.timetz().replace(tzinfo=None) if parsed.tzinfo else parsed.time()
    parts = text.split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    second = int(parts[2]) if len(parts) > 2 else 0
    return time(hour, minute, second)
