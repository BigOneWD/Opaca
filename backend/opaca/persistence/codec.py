"""Lossless SQLite codecs for Decimal and timezone-aware timestamps."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from opaca.domain.money import MoneyError, money


class CodecError(ValueError):
    """Raised when persisted state cannot be decoded losslessly."""


def dump_decimal(value: Decimal) -> str:
    return format(value, "f")


def load_decimal(value: str) -> Decimal:
    try:
        return money(value)
    except MoneyError as exc:
        raise CodecError(f"invalid decimal {value!r}") from exc


def dump_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise CodecError(f"naive datetime is forbidden: {value!r}")
    return value.astimezone(UTC).isoformat()


def load_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CodecError(f"invalid datetime {value!r}") from exc
    if parsed.tzinfo is None:
        raise CodecError(f"naive datetime is forbidden: {value!r}")
    return parsed.astimezone(UTC)


def dump_date(value: date) -> str:
    return value.isoformat()


def load_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CodecError(f"invalid date {value!r}") from exc
