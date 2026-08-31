"""Read-only market-data surface. Separate from the mutating paper gateway.

PAPER credentials stay environment-only. This module never submits, cancels,
or retains a TradingClient. There is no fallback to synthetic prices.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol, cast, runtime_checkable

from opaca.broker.adapters import parse_datetime_field
from opaca.broker.errors import PaperEnvironmentError
from opaca.broker.paper import load_paper_credentials
from opaca.domain.money import MoneyError, positive_money, require_positive_decimal
from opaca.market.errors import MarketDataUnavailableError, QuoteValidationError
from opaca.market.quote import (
    QUOTE_SOURCE_LATEST_TRADE,
    CanonicalMarketPrice,
    require_permitted_symbol,
)


@runtime_checkable
class ReadOnlyMarketData(Protocol):
    """Narrow read-only quote capability for the permitted ETF universe."""

    def get_latest_trade(self, symbol: str) -> CanonicalMarketPrice: ...


class _DataReadClient(Protocol):
    def get_stock_latest_trade(self, request: object = ...) -> object: ...


def _decimal_from_sdk(value: object, *, field_name: str) -> Decimal:
    """Coerce an SDK price to a validated Decimal. Bool/None/NaN fail closed.

    alpaca-py may return a binary float. That float is converted through a
    fixed decimal string and then validated; it is never used as a Python
    float in arithmetic. Synthetic fixture prices are never substituted.
    """
    if isinstance(value, bool) or value is None:
        raise QuoteValidationError(f"{field_name} is not a price")
    if isinstance(value, Decimal):
        try:
            return require_positive_decimal(value)
        except MoneyError as exc:
            raise QuoteValidationError(str(exc)) from exc
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return positive_money(value)
        except MoneyError as exc:
            raise QuoteValidationError(str(exc)) from exc
    if isinstance(value, str):
        try:
            return positive_money(value)
        except MoneyError as exc:
            raise QuoteValidationError(str(exc)) from exc
    if isinstance(value, float):
        if value != value or value == float("inf") or value == float("-inf"):
            raise QuoteValidationError(f"{field_name} is non-finite")
        try:
            return positive_money(format(value, ".8f"))
        except MoneyError as exc:
            raise QuoteValidationError(str(exc)) from exc
    raise QuoteValidationError(f"{field_name} has unsupported type {type(value).__name__}")


def _extract_trade(payload: object, symbol: str) -> object:
    if payload is None:
        raise MarketDataUnavailableError(f"no latest trade for {symbol}")
    if isinstance(payload, Mapping):
        if symbol in payload:
            trade = payload[symbol]
            if trade is None:
                raise MarketDataUnavailableError(f"no latest trade for {symbol}")
            return trade
        raise MarketDataUnavailableError(f"no latest trade for {symbol}")
    getter = getattr(payload, "get", None)
    if callable(getter):
        trade = getter(symbol)
        if trade is not None:
            return trade
    try:
        trade = payload[symbol]  # type: ignore[index]
    except Exception as exc:
        raise MarketDataUnavailableError(f"no latest trade for {symbol}") from exc
    if trade is None:
        raise MarketDataUnavailableError(f"no latest trade for {symbol}")
    return trade


def _trade_price_and_timestamp(trade: object) -> tuple[object, object]:
    if isinstance(trade, Mapping):
        return trade.get("price"), trade.get("timestamp")
    dumped = getattr(trade, "model_dump", None)
    if callable(dumped):
        raw = dumped()
        if isinstance(raw, Mapping):
            return raw.get("price"), raw.get("timestamp")
    return getattr(trade, "price", None), getattr(trade, "timestamp", None)


@dataclass
class FakeMarketData:
    """Deterministic offline double. Never talks to Alpaca."""

    quotes: Mapping[str, CanonicalMarketPrice] = field(default_factory=dict)
    unavailable: bool = False

    def get_latest_trade(self, symbol: str) -> CanonicalMarketPrice:
        require_permitted_symbol(symbol)
        if self.unavailable:
            raise MarketDataUnavailableError("market data unavailable")
        quote = self.quotes.get(symbol)
        if quote is None:
            raise MarketDataUnavailableError(f"no latest trade for {symbol}")
        return quote


class AlpacaPaperMarketData:
    """Read-only Alpaca stock latest-trade source. No TradingClient. No mutation.

    Bound read callables are captured at construction; no ``_client`` attribute
    exists on the instance.
    """

    __slots__ = ("_get_stock_latest_trade",)

    def __init__(self, client: object) -> None:
        reader = cast(_DataReadClient, client)
        get_trade: Callable[..., object] = reader.get_stock_latest_trade
        self._get_stock_latest_trade = get_trade

    def get_latest_trade(self, symbol: str) -> CanonicalMarketPrice:
        require_permitted_symbol(symbol)
        try:
            from alpaca.data.enums import DataFeed
            from alpaca.data.requests import StockLatestTradeRequest

            request = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
            payload = self._get_stock_latest_trade(request)
        except PaperEnvironmentError:
            raise
        except QuoteValidationError:
            raise
        except MarketDataUnavailableError:
            raise
        except Exception as exc:
            raise MarketDataUnavailableError(f"latest trade unavailable for {symbol}") from exc
        trade = _extract_trade(payload, symbol)
        raw_price, raw_timestamp = _trade_price_and_timestamp(trade)
        try:
            price = _decimal_from_sdk(raw_price, field_name="trade.price")
            timestamp = parse_datetime_field(raw_timestamp, "trade.timestamp")
        except QuoteValidationError:
            raise
        except Exception as exc:
            raise MarketDataUnavailableError(f"latest trade unusable for {symbol}") from exc
        fetched_at = datetime.now(UTC)
        return CanonicalMarketPrice(
            symbol=symbol,
            price=price,
            source_timestamp=timestamp,
            fetched_at=fetched_at,
            source=QUOTE_SOURCE_LATEST_TRADE,
        )


def open_paper_market_data_from_env() -> AlpacaPaperMarketData:
    """Construct a read-only data client from process environment credentials."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
    except ImportError as exc:
        raise PaperEnvironmentError("alpaca-py is required for live paper market data") from exc
    key_id, secret = load_paper_credentials()
    client = StockHistoricalDataClient(api_key=key_id, secret_key=secret)
    return AlpacaPaperMarketData(client)


def latest_trades(
    source: ReadOnlyMarketData,
    symbols: tuple[str, ...],
) -> dict[str, CanonicalMarketPrice]:
    quotes: dict[str, CanonicalMarketPrice] = {}
    for symbol in symbols:
        quotes[symbol] = source.get_latest_trade(symbol)
    return quotes
