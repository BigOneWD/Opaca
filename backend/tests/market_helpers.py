"""Offline builders for canonical quotes. Not production prices."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from opaca.market.quote import QUOTE_SOURCE_LATEST_TRADE, CanonicalMarketPrice

from tests.helpers import DEFAULT_NOW, DEFAULT_PRICES


def canonical_quote(
    symbol: str = "SGOV",
    price: Decimal | str = "100.69",
    *,
    now: datetime = DEFAULT_NOW,
    age_seconds: int = 1,
    source: str = QUOTE_SOURCE_LATEST_TRADE,
) -> CanonicalMarketPrice:
    amount = price if isinstance(price, Decimal) else Decimal(price)
    return CanonicalMarketPrice(
        symbol=symbol,
        price=amount,
        source_timestamp=now - timedelta(seconds=age_seconds),
        fetched_at=now,
        source=source,
    )


def universe_quotes(
    *,
    now: datetime = DEFAULT_NOW,
    age_seconds: int = 1,
    sgov: Decimal | str | None = None,
) -> dict[str, CanonicalMarketPrice]:
    sgov_price = DEFAULT_PRICES["SGOV"] if sgov is None else sgov
    return {
        "SGOV": canonical_quote("SGOV", sgov_price, now=now, age_seconds=age_seconds),
        "BIL": canonical_quote("BIL", DEFAULT_PRICES["BIL"], now=now, age_seconds=age_seconds),
        "SHV": canonical_quote("SHV", DEFAULT_PRICES["SHV"], now=now, age_seconds=age_seconds),
    }
