"""Canonical validated market price for SGOV / BIL / SHV.

The production live-paper path must consume this object — never
offline fixture constants or a caller-supplied arbitrary
``reference_price``. Unavailable, stale, future, or malformed quotes
fail closed. There is no synthetic fallback.

Live PAPER executable prices are derived from Alpaca IEX latest quotes:
BUY uses ask, SELL uses bid. Held permitted inventory needed only for
valuation is marked at the bid (conservative executable sell).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from opaca.broker.gateway import ASSET_UNIVERSE
from opaca.domain.models import Position, Side
from opaca.domain.money import (
    MAGNITUDE_LIMIT,
    ZERO,
    MoneyError,
    non_negative_money,
    require_positive_decimal,
)
from opaca.market.errors import (
    FutureQuoteError,
    QuoteValidationError,
    StaleQuoteError,
)

QUOTE_SOURCE_LATEST_QUOTE_IEX = "alpaca.stock.latest_quote.iex"

#: How recently Opaca obtained the latest quote (``now - fetched_at``).
#: This is the hard IEX-latest-quote freshness control at decision and
#: mutation boundaries. Override per call; do not silently extend.
DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS = 15


@dataclass(frozen=True)
class CanonicalMarketPrice:
    """One validated print used as the sole live-paper price source."""

    symbol: str
    price: Decimal
    source_timestamp: datetime
    fetched_at: datetime
    source: str

    def __post_init__(self) -> None:
        require_permitted_symbol(self.symbol)
        try:
            object.__setattr__(self, "price", require_positive_decimal(self.price))
        except MoneyError as exc:
            raise QuoteValidationError(str(exc)) from exc
        if self.source_timestamp.tzinfo is None:
            raise QuoteValidationError("quote source_timestamp is naive; fail closed")
        if self.fetched_at.tzinfo is None:
            raise QuoteValidationError("quote fetched_at is naive; fail closed")
        if not self.source:
            raise QuoteValidationError("quote source must be non-empty")


@dataclass(frozen=True)
class IexLatestQuote:
    """Validated Alpaca IEX NBBO-style quote. Not a last trade."""

    symbol: str
    bid_price: Decimal
    ask_price: Decimal
    bid_size: Decimal
    ask_size: Decimal
    source_timestamp: datetime
    fetched_at: datetime
    source: str

    def __post_init__(self) -> None:
        require_permitted_symbol(self.symbol)
        try:
            object.__setattr__(self, "bid_price", require_positive_decimal(self.bid_price))
            object.__setattr__(self, "ask_price", require_positive_decimal(self.ask_price))
            object.__setattr__(self, "bid_size", non_negative_money(self.bid_size))
            object.__setattr__(self, "ask_size", non_negative_money(self.ask_size))
        except MoneyError as exc:
            raise QuoteValidationError(str(exc)) from exc
        if self.bid_price > self.ask_price:
            raise QuoteValidationError(
                f"bid {self.bid_price} exceeds ask {self.ask_price} for {self.symbol}; fail closed"
            )
        if self.source_timestamp.tzinfo is None:
            raise QuoteValidationError("quote source_timestamp is naive; fail closed")
        if self.fetched_at.tzinfo is None:
            raise QuoteValidationError("quote fetched_at is naive; fail closed")
        if self.source != QUOTE_SOURCE_LATEST_QUOTE_IEX:
            raise QuoteValidationError(
                f"quote source must be {QUOTE_SOURCE_LATEST_QUOTE_IEX}; got {self.source!r}"
            )


def require_permitted_symbol(symbol: str) -> str:
    if not symbol:
        raise QuoteValidationError("symbol must be non-empty")
    if symbol not in ASSET_UNIVERSE:
        raise QuoteValidationError(
            f"symbol {symbol!r} is not in the permitted universe {ASSET_UNIVERSE}"
        )
    return symbol


def required_pricing_symbols(
    proposal_symbols: Sequence[str],
    positions: Sequence[Position],
    *,
    permitted_symbols: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Symbols that must have a fresh IEX quote for the current decision.

    Required: every proposal symbol, plus every currently held permitted
    asset whose value feeds TreasuryGuard / concentration / projection.
    Unused whitelist names with zero position do not block.
    """
    permitted = ASSET_UNIVERSE if permitted_symbols is None else permitted_symbols
    required: set[str] = set()
    for symbol in proposal_symbols:
        required.add(require_permitted_symbol(symbol))
    for position in positions:
        if position.quantity <= ZERO:
            continue
        if position.symbol not in permitted:
            continue
        if position.symbol not in ASSET_UNIVERSE:
            continue
        required.add(require_permitted_symbol(position.symbol))
    return tuple(sorted(required))


def executable_canonical_price(quote: IexLatestQuote, side: Side) -> CanonicalMarketPrice:
    """BUY binds to ask; SELL binds to bid. Zero executable size fails closed."""
    if side is Side.BUY:
        if quote.ask_size <= ZERO:
            raise QuoteValidationError(f"BUY ask_size is unusable for {quote.symbol}; fail closed")
        price = quote.ask_price
    elif side is Side.SELL:
        if quote.bid_size <= ZERO:
            raise QuoteValidationError(f"SELL bid_size is unusable for {quote.symbol}; fail closed")
        price = quote.bid_price
    else:
        raise QuoteValidationError(f"unsupported side {side!r}")
    return CanonicalMarketPrice(
        symbol=quote.symbol,
        price=price,
        source_timestamp=quote.source_timestamp,
        fetched_at=quote.fetched_at,
        source=quote.source,
    )


def conservative_long_valuation_price(quote: IexLatestQuote) -> CanonicalMarketPrice:
    """Held long inventory is marked at the IEX bid (conservative executable sell)."""
    return executable_canonical_price(quote, Side.SELL)


def canonical_prices_for_decision(
    quotes: Mapping[str, IexLatestQuote],
    *,
    side_by_symbol: Mapping[str, Side],
) -> dict[str, CanonicalMarketPrice]:
    """Map IEX quotes to side-specific canonical prices. Held-only names use bid."""
    out: dict[str, CanonicalMarketPrice] = {}
    for symbol, quote in quotes.items():
        if quote.symbol != symbol:
            raise QuoteValidationError(f"wrong symbol {quote.symbol!r} for key {symbol!r}")
        side = side_by_symbol.get(symbol)
        if side is None:
            out[symbol] = conservative_long_valuation_price(quote)
        else:
            out[symbol] = executable_canonical_price(quote, side)
    return out


def quote_fetch_age_seconds(
    quote: CanonicalMarketPrice | IexLatestQuote, *, now: datetime
) -> float:
    if now.tzinfo is None:
        raise QuoteValidationError("evaluation now is naive; fail closed")
    return (now - quote.fetched_at).total_seconds()


def quote_source_event_age_seconds(
    quote: CanonicalMarketPrice | IexLatestQuote, *, now: datetime
) -> float:
    if now.tzinfo is None:
        raise QuoteValidationError("evaluation now is naive; fail closed")
    return (now - quote.source_timestamp).total_seconds()


def quote_freshness_diagnostics(
    quote: CanonicalMarketPrice | IexLatestQuote, *, now: datetime
) -> str:
    """Fetch age is authoritative; source-event age is diagnostic only."""
    fetch_age = quote_fetch_age_seconds(quote, now=now)
    source_age = quote_source_event_age_seconds(quote, now=now)
    return f"fetch_age_seconds={fetch_age}; source_event_age_seconds={source_age} (diagnostic)"


def validate_quote_freshness(
    quote: CanonicalMarketPrice | IexLatestQuote,
    *,
    now: datetime,
    max_fetch_age_seconds: int = DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS,
) -> None:
    """Fail closed on future timestamps or a stale Alpaca fetch.

    Fetch freshness (``now - fetched_at``) is the hard IEX-latest-quote
    control. Inclusive: fetch age equal to the configured maximum is
    accepted; one microsecond beyond is not.

    Source-event age (``now - source_timestamp``) is diagnostic for IEX
    latest-BBO. A freshly fetched quote may carry an older exchange event
    if IEX has emitted no newer BBO. A future ``source_timestamp`` remains
    invalid.
    """
    if max_fetch_age_seconds <= 0:
        raise QuoteValidationError("max_fetch_age_seconds must be > 0")
    if now.tzinfo is None:
        raise QuoteValidationError("evaluation now is naive; fail closed")
    if quote.fetched_at > now:
        raise FutureQuoteError(
            f"quote fetched_at {quote.fetched_at.isoformat()} is in the future "
            f"relative to {now.isoformat()}; fail closed"
        )
    if quote.source_timestamp > now:
        raise FutureQuoteError(
            f"quote source_timestamp {quote.source_timestamp.isoformat()} is in the future "
            f"relative to {now.isoformat()}; fail closed"
        )
    fetch_age = quote_fetch_age_seconds(quote, now=now)
    if fetch_age > max_fetch_age_seconds:
        raise StaleQuoteError(
            f"quote fetch age {fetch_age}s exceeds max {max_fetch_age_seconds}s; fail closed"
        )


def validate_iex_latest_quote(
    quote: IexLatestQuote,
    *,
    now: datetime,
    max_fetch_age_seconds: int = DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS,
) -> IexLatestQuote:
    require_permitted_symbol(quote.symbol)
    if quote.source != QUOTE_SOURCE_LATEST_QUOTE_IEX:
        raise QuoteValidationError(
            f"quote source must be {QUOTE_SOURCE_LATEST_QUOTE_IEX}; got {quote.source!r}"
        )
    validate_quote_freshness(
        quote,
        now=now,
        max_fetch_age_seconds=max_fetch_age_seconds,
    )
    return quote


def validate_canonical_quote(
    quote: CanonicalMarketPrice,
    *,
    now: datetime,
    max_fetch_age_seconds: int = DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS,
) -> CanonicalMarketPrice:
    require_permitted_symbol(quote.symbol)
    if quote.price <= 0 or not quote.price.is_finite() or quote.price >= MAGNITUDE_LIMIT:
        raise QuoteValidationError(f"malformed canonical price {quote.price!r}")
    validate_quote_freshness(
        quote,
        now=now,
        max_fetch_age_seconds=max_fetch_age_seconds,
    )
    return quote
