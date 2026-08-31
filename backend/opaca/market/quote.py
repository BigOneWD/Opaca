"""Canonical validated market price for SGOV / BIL / SHV.

The production live-paper path must consume this object — never
offline fixture constants or a caller-supplied arbitrary
``reference_price``. Unavailable, stale, future, or malformed quotes
fail closed. There is no synthetic fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from opaca.broker.gateway import ASSET_UNIVERSE
from opaca.domain.money import MAGNITUDE_LIMIT, MoneyError, require_positive_decimal
from opaca.market.errors import (
    FutureQuoteError,
    QuoteValidationError,
    StaleQuoteError,
)

QUOTE_SOURCE_LATEST_TRADE = "alpaca.stock.latest_trade"

#: Maximum age of a quote source timestamp relative to evaluation ``now``.
#: 15s is tighter than snapshot freshness (60s) because a limit price is an
#: economic bound, not merely a book snapshot. Override per call; do not
#: silently extend.
DEFAULT_MAX_QUOTE_AGE_SECONDS = 15


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


def require_permitted_symbol(symbol: str) -> str:
    if not symbol:
        raise QuoteValidationError("symbol must be non-empty")
    if symbol not in ASSET_UNIVERSE:
        raise QuoteValidationError(
            f"symbol {symbol!r} is not in the permitted universe {ASSET_UNIVERSE}"
        )
    return symbol


def quote_age_seconds(quote: CanonicalMarketPrice, *, now: datetime) -> float:
    if now.tzinfo is None:
        raise QuoteValidationError("evaluation now is naive; fail closed")
    return (now - quote.source_timestamp).total_seconds()


def validate_quote_freshness(
    quote: CanonicalMarketPrice,
    *,
    now: datetime,
    max_age_seconds: int = DEFAULT_MAX_QUOTE_AGE_SECONDS,
) -> None:
    """Fail closed on future or stale quotes. ``max_age_seconds`` is required
    to be a positive configured bound — never an implicit infinite window."""
    if max_age_seconds <= 0:
        raise QuoteValidationError("max_age_seconds must be > 0")
    if now.tzinfo is None:
        raise QuoteValidationError("evaluation now is naive; fail closed")
    if quote.source_timestamp > now:
        raise FutureQuoteError(
            f"quote timestamp {quote.source_timestamp.isoformat()} is in the future "
            f"relative to {now.isoformat()}; fail closed"
        )
    age = quote_age_seconds(quote, now=now)
    if age > max_age_seconds:
        raise StaleQuoteError(f"quote age {age}s exceeds max {max_age_seconds}s; fail closed")


def validate_canonical_quote(
    quote: CanonicalMarketPrice,
    *,
    now: datetime,
    max_age_seconds: int = DEFAULT_MAX_QUOTE_AGE_SECONDS,
) -> CanonicalMarketPrice:
    require_permitted_symbol(quote.symbol)
    if quote.price <= 0 or not quote.price.is_finite() or quote.price >= MAGNITUDE_LIMIT:
        raise QuoteValidationError(f"malformed canonical price {quote.price!r}")
    validate_quote_freshness(quote, now=now, max_age_seconds=max_age_seconds)
    return quote
