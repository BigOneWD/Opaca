"""Fail-closed adapters for Alpaca option payloads."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal

from opaca.broker.adapters import as_mapping, parse_datetime_field
from opaca.broker.errors import InvalidBrokerStateError
from opaca.domain.money import MoneyError, money, non_negative_money, positive_money
from opaca.wheel.models import (
    OptionContract,
    OptionPosition,
    OptionQuote,
    OptionRight,
)


def _required(data: Mapping[str, object], *keys: str) -> object:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    raise InvalidBrokerStateError(f"missing option field {keys[0]!r}")


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidBrokerStateError(f"option field {name!r} is empty")
    return value


def _token(value: object, name: str) -> str:
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        token = enum_value.strip().lower()
    elif isinstance(value, str):
        token = value.strip().lower()
    else:
        token = ""
    if not token:
        raise InvalidBrokerStateError(f"option field {name!r} is malformed")
    return token


def _decimal(value: object, name: str, *, allow_negative: bool = False) -> Decimal:
    if isinstance(value, (float, bool)):
        raise InvalidBrokerStateError(f"option field {name!r} is not an exact decimal")
    if not isinstance(value, str | int | Decimal):
        raise InvalidBrokerStateError(f"option field {name!r} is not an exact decimal")
    try:
        return money(value) if allow_negative else non_negative_money(value)
    except (MoneyError, TypeError, ValueError) as exc:
        raise InvalidBrokerStateError(f"option field {name!r} is not a valid decimal") from exc


def _date(value: object, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise InvalidBrokerStateError(f"option field {name!r} is not a date")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise InvalidBrokerStateError(f"option field {name!r} is not a date") from exc


def _option_right(data: Mapping[str, object]) -> OptionRight:
    right = _token(_required(data, "type", "right"), "type")
    if right != OptionRight.PUT.value.lower():
        raise InvalidBrokerStateError("only PUT option contracts are supported")
    return OptionRight.PUT


def adapt_option_contract(raw: object) -> OptionContract:
    """Adapt one authoritative Alpaca option-contract payload."""
    data = as_mapping(raw)
    try:
        status = _token(_required(data, "status"), "status")
        tradable = data.get("tradable")
        if not isinstance(tradable, bool):
            raise InvalidBrokerStateError("option contract tradable must be bool")
        return OptionContract(
            occ_symbol=_text(_required(data, "symbol"), "symbol"),
            underlying=_text(_required(data, "underlying_symbol", "underlying"), "underlying"),
            right=_option_right(data),
            strike=positive_money(_decimal(_required(data, "strike_price", "strike"), "strike")),
            expiration=_date(_required(data, "expiration_date", "expiration"), "expiration"),
            multiplier=positive_money(_decimal(_required(data, "multiplier"), "multiplier")),
            active=status == "active",
            tradable=tradable,
        )
    except InvalidBrokerStateError:
        raise
    except (MoneyError, TypeError, ValueError) as exc:
        raise InvalidBrokerStateError("malformed option contract") from exc


def adapt_option_quote(raw: object) -> OptionQuote:
    """Adapt an option quote without applying freshness policy."""
    data = as_mapping(raw)
    try:
        timestamp = parse_datetime_field(_required(data, "timestamp", "as_of"), "timestamp")
        return OptionQuote(
            bid=_decimal(_required(data, "bid_price", "bid"), "bid"),
            ask=_decimal(_required(data, "ask_price", "ask"), "ask"),
            as_of=timestamp,
        )
    except InvalidBrokerStateError:
        raise
    except (MoneyError, TypeError, ValueError) as exc:
        raise InvalidBrokerStateError("malformed option quote") from exc


def _option_quantity(value: object) -> int:
    quantity = _decimal(value, "quantity", allow_negative=True)
    absolute = quantity.copy_abs()
    if absolute <= 0 or absolute != absolute.to_integral_value():
        raise InvalidBrokerStateError("option quantity must be a positive integer")
    return int(absolute)


def adapt_option_position(raw: object) -> OptionPosition:
    """Adapt a long or short option position without using equity semantics."""
    data = as_mapping(raw)
    try:
        side = _token(_required(data, "side"), "side").upper()
        if side not in {"LONG", "SHORT"}:
            raise InvalidBrokerStateError("option position side is malformed")
        return OptionPosition(
            occ_symbol=_text(_required(data, "symbol", "occ_symbol"), "symbol"),
            underlying=_text(_required(data, "underlying_symbol", "underlying"), "underlying"),
            right=_option_right(data),
            contracts=_option_quantity(_required(data, "qty", "quantity")),
            side=side,
        )
    except InvalidBrokerStateError:
        raise
    except (MoneyError, TypeError, ValueError) as exc:
        raise InvalidBrokerStateError("malformed option position") from exc
