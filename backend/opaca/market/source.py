"""Read-only market-data surface. Separate from the mutating paper gateway.

PAPER credentials stay environment-only. This module never submits, cancels,
or retains a TradingClient. There is no fallback to synthetic prices, last
trade, or a non-IEX feed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol, cast, runtime_checkable

from opaca.broker.adapters import parse_datetime_field
from opaca.broker.errors import InvalidBrokerStateError, PaperEnvironmentError
from opaca.broker.paper import load_paper_credentials
from opaca.domain.models import Position, Side
from opaca.domain.money import (
    MoneyError,
    non_negative_money,
    positive_money,
    require_positive_decimal,
)
from opaca.market.errors import FutureQuoteError, MarketDataUnavailableError, QuoteValidationError
from opaca.market.quote import (
    DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS,
    QUOTE_SOURCE_LATEST_QUOTE_IEX,
    CanonicalMarketPrice,
    IexLatestQuote,
    canonical_prices_for_decision,
    require_permitted_symbol,
    required_pricing_symbols,
    validate_canonical_quote,
)


@runtime_checkable
class ReadOnlyMarketData(Protocol):
    """Narrow read-only IEX quote capability for the permitted ETF universe."""

    def get_latest_quote(self, symbol: str) -> IexLatestQuote: ...


class _DataReadClient(Protocol):
    def get_stock_latest_quote(self, request: object = ...) -> object: ...


def _decimal_from_sdk(value: object, *, field_name: str, allow_zero: bool = False) -> Decimal:
    """Coerce an SDK number to a validated Decimal. Bool/None/NaN fail closed.

    alpaca-py may return a binary float. That float is converted through a
    fixed decimal string and then validated; it is never used as a Python
    float in arithmetic. Synthetic fixture prices are never substituted.
    """
    if isinstance(value, bool) or value is None:
        raise QuoteValidationError(f"{field_name} is not a valid decimal")
    if isinstance(value, Decimal):
        try:
            if allow_zero:
                return non_negative_money(value)
            return require_positive_decimal(value)
        except MoneyError as exc:
            raise QuoteValidationError(str(exc)) from exc
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return non_negative_money(value) if allow_zero else positive_money(value)
        except MoneyError as exc:
            raise QuoteValidationError(str(exc)) from exc
    if isinstance(value, str):
        try:
            return non_negative_money(value) if allow_zero else positive_money(value)
        except MoneyError as exc:
            raise QuoteValidationError(str(exc)) from exc
    if isinstance(value, float):
        if value != value or value == float("inf") or value == float("-inf"):
            raise QuoteValidationError(f"{field_name} is non-finite")
        try:
            text = format(value, ".8f")
            return non_negative_money(text) if allow_zero else positive_money(text)
        except MoneyError as exc:
            raise QuoteValidationError(str(exc)) from exc
    raise QuoteValidationError(f"{field_name} has unsupported type {type(value).__name__}")


def _as_mapping(payload: object) -> Mapping[str, object] | None:
    if isinstance(payload, Mapping):
        return payload
    dumped = getattr(payload, "model_dump", None)
    if callable(dumped):
        raw = dumped()
        if isinstance(raw, Mapping):
            return raw
    return None


def _extract_quote(payload: object, symbol: str) -> object:
    if payload is None:
        raise MarketDataUnavailableError(f"no latest quote for {symbol}")
    mapping = _as_mapping(payload)
    if mapping is not None:
        if symbol in mapping:
            quote = mapping[symbol]
            if quote is None:
                raise MarketDataUnavailableError(f"no latest quote for {symbol}")
            return quote
        if "bid_price" in mapping or "ask_price" in mapping:
            return mapping
        raise MarketDataUnavailableError(f"no latest quote for {symbol}")
    getter = getattr(payload, "get", None)
    if callable(getter):
        quote = getter(symbol)
        if quote is not None:
            return quote
    try:
        quote = payload[symbol]  # type: ignore[index]
    except Exception:
        if getattr(payload, "bid_price", None) is not None or hasattr(payload, "bid_price"):
            return payload
        raise MarketDataUnavailableError(f"no latest quote for {symbol}") from None
    if quote is None:
        raise MarketDataUnavailableError(f"no latest quote for {symbol}")
    return quote


def _quote_fields(quote: object) -> Mapping[str, object]:
    mapping = _as_mapping(quote)
    if mapping is not None:
        return mapping
    return {
        "symbol": getattr(quote, "symbol", None),
        "bid_price": getattr(quote, "bid_price", None),
        "ask_price": getattr(quote, "ask_price", None),
        "bid_size": getattr(quote, "bid_size", None),
        "ask_size": getattr(quote, "ask_size", None),
        "timestamp": getattr(quote, "timestamp", None),
        "feed": getattr(quote, "feed", None),
    }


def _reject_non_iex_feed(fields: Mapping[str, object]) -> None:
    feed = fields.get("feed")
    if feed is None:
        return
    token = str(getattr(feed, "value", feed)).strip().lower()
    if token in {"iex", "i"}:
        return
    raise QuoteValidationError(f"non-IEX quote feed {token!r}; fail closed")


def _reject_wrong_symbol(fields: Mapping[str, object], symbol: str) -> None:
    inner = fields.get("symbol")
    if inner is None:
        return
    text = str(getattr(inner, "value", inner)).strip().upper()
    if not text:
        return
    if text != symbol:
        raise QuoteValidationError(f"wrong symbol {text!r}; expected {symbol}")


@dataclass
class FakeMarketData:
    """Deterministic offline double. Never talks to Alpaca."""

    quotes: Mapping[str, IexLatestQuote] = field(default_factory=dict)
    unavailable: bool = False
    fetch_log: list[str] = field(default_factory=list)
    after_fetch: Callable[[], None] | None = None

    def get_latest_quote(self, symbol: str) -> IexLatestQuote:
        require_permitted_symbol(symbol)
        self.fetch_log.append(symbol)
        if self.unavailable:
            raise MarketDataUnavailableError("market data unavailable")
        quote = self.quotes.get(symbol)
        if quote is None:
            raise MarketDataUnavailableError(f"no latest quote for {symbol}")
        if quote.symbol != symbol:
            raise QuoteValidationError(f"wrong symbol {quote.symbol!r}; expected {symbol}")
        if self.after_fetch is not None:
            self.after_fetch()
        return quote


class AlpacaPaperMarketData:
    """Read-only Alpaca IEX latest-quote source. No TradingClient. No mutation.

    Bound read callables are captured at construction; no ``_client`` attribute
    exists on the instance.
    """

    __slots__ = ("_get_stock_latest_quote",)

    def __init__(self, client: object) -> None:
        reader = cast(_DataReadClient, client)
        get_quote: Callable[..., object] = reader.get_stock_latest_quote
        self._get_stock_latest_quote = get_quote

    def get_latest_quote(self, symbol: str) -> IexLatestQuote:
        require_permitted_symbol(symbol)
        try:
            from alpaca.data.enums import DataFeed
            from alpaca.data.requests import StockLatestQuoteRequest

            request = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
            payload = self._get_stock_latest_quote(request)
        except PaperEnvironmentError:
            raise
        except QuoteValidationError:
            raise
        except MarketDataUnavailableError:
            raise
        except Exception as exc:
            raise MarketDataUnavailableError(f"latest quote unavailable for {symbol}") from exc
        quote = _extract_quote(payload, symbol)
        fields = _quote_fields(quote)
        try:
            _reject_non_iex_feed(fields)
            _reject_wrong_symbol(fields, symbol)
            bid_price = _decimal_from_sdk(fields.get("bid_price"), field_name="quote.bid_price")
            ask_price = _decimal_from_sdk(fields.get("ask_price"), field_name="quote.ask_price")
            bid_size = _decimal_from_sdk(
                fields.get("bid_size"), field_name="quote.bid_size", allow_zero=True
            )
            ask_size = _decimal_from_sdk(
                fields.get("ask_size"), field_name="quote.ask_size", allow_zero=True
            )
            try:
                timestamp = parse_datetime_field(fields.get("timestamp"), "quote.timestamp")
            except InvalidBrokerStateError as exc:
                raise QuoteValidationError(str(exc)) from exc
        except QuoteValidationError:
            raise
        except MarketDataUnavailableError:
            raise
        except Exception as exc:
            raise QuoteValidationError(f"malformed quote payload for {symbol}") from exc
        fetched_at = datetime.now(UTC)
        return IexLatestQuote(
            symbol=symbol,
            bid_price=bid_price,
            ask_price=ask_price,
            bid_size=bid_size,
            ask_size=ask_size,
            source_timestamp=timestamp,
            fetched_at=fetched_at,
            source=QUOTE_SOURCE_LATEST_QUOTE_IEX,
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


def latest_quotes(
    source: ReadOnlyMarketData,
    symbols: Sequence[str],
) -> dict[str, IexLatestQuote]:
    quotes: dict[str, IexLatestQuote] = {}
    for symbol in symbols:
        quotes[symbol] = source.get_latest_quote(symbol)
    return quotes


def required_canonical_prices(
    source: ReadOnlyMarketData,
    *,
    proposal_symbols: Sequence[str],
    side_by_symbol: Mapping[str, Side],
    positions: Sequence[Position],
    now: datetime,
    max_fetch_age_seconds: int = DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS,
    permitted_symbols: frozenset[str] | None = None,
) -> dict[str, CanonicalMarketPrice]:
    """Fetch fresh IEX quotes for economically required symbols only."""
    for symbol in proposal_symbols:
        if symbol not in side_by_symbol:
            raise QuoteValidationError(f"missing executable side for {symbol}")
    required = required_pricing_symbols(
        proposal_symbols, positions, permitted_symbols=permitted_symbols
    )
    fetched = latest_quotes(source, required)
    for symbol in required:
        if symbol not in fetched:
            raise MarketDataUnavailableError(f"no latest quote for {symbol}")
    decision_now = now
    latest_fetch = max(item.fetched_at for item in fetched.values())
    forward_fetch_age = latest_fetch - now
    if forward_fetch_age > timedelta(seconds=max_fetch_age_seconds):
        raise FutureQuoteError(
            f"latest quote fetched_at {latest_fetch.isoformat()} is "
            f"{forward_fetch_age.total_seconds()}s in the future relative to supplied now "
            f"and exceeds max fetch age {max_fetch_age_seconds}s; fail closed"
        )
    if latest_fetch > decision_now:
        decision_now = latest_fetch
    canonical = canonical_prices_for_decision(fetched, side_by_symbol=side_by_symbol)
    for symbol in required:
        if symbol not in canonical:
            raise MarketDataUnavailableError(f"no executable price for {symbol}")
        validate_canonical_quote(
            canonical[symbol],
            now=decision_now,
            max_fetch_age_seconds=max_fetch_age_seconds,
        )
    return canonical
