"""Offline builders for canonical quotes. Not production prices."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal

from opaca.market.binding import BoundExecutionPrice
from opaca.market.quote import (
    QUOTE_SOURCE_LATEST_QUOTE_IEX,
    CanonicalMarketPrice,
    IexLatestQuote,
)
from opaca.market.source import FakeMarketData

from tests.helpers import DEFAULT_NOW, DEFAULT_PRICES


def canonical_quote(
    symbol: str = "SGOV",
    price: Decimal | str = "100.69",
    *,
    now: datetime = DEFAULT_NOW,
    age_seconds: int = 1,
    fetch_age_seconds: int = 0,
    source_event_age_seconds: int | None = None,
    source: str = QUOTE_SOURCE_LATEST_QUOTE_IEX,
) -> CanonicalMarketPrice:
    amount = price if isinstance(price, Decimal) else Decimal(price)
    source_age = age_seconds if source_event_age_seconds is None else source_event_age_seconds
    return CanonicalMarketPrice(
        symbol=symbol,
        price=amount,
        source_timestamp=now - timedelta(seconds=source_age),
        fetched_at=now - timedelta(seconds=fetch_age_seconds),
        source=source,
    )


def universe_quotes(
    *,
    now: datetime = DEFAULT_NOW,
    age_seconds: int = 1,
    fetch_age_seconds: int = 0,
    source_event_age_seconds: int | None = None,
    sgov: Decimal | str | None = None,
) -> dict[str, CanonicalMarketPrice]:
    sgov_price = DEFAULT_PRICES["SGOV"] if sgov is None else sgov
    return {
        "SGOV": canonical_quote(
            "SGOV",
            sgov_price,
            now=now,
            age_seconds=age_seconds,
            fetch_age_seconds=fetch_age_seconds,
            source_event_age_seconds=source_event_age_seconds,
        ),
        "BIL": canonical_quote(
            "BIL",
            DEFAULT_PRICES["BIL"],
            now=now,
            age_seconds=age_seconds,
            fetch_age_seconds=fetch_age_seconds,
            source_event_age_seconds=source_event_age_seconds,
        ),
        "SHV": canonical_quote(
            "SHV",
            DEFAULT_PRICES["SHV"],
            now=now,
            age_seconds=age_seconds,
            fetch_age_seconds=fetch_age_seconds,
            source_event_age_seconds=source_event_age_seconds,
        ),
    }


def iex_quote(
    symbol: str = "SGOV",
    *,
    bid: Decimal | str = "100.69",
    ask: Decimal | str = "100.70",
    bid_size: Decimal | str = "1",
    ask_size: Decimal | str = "1",
    now: datetime = DEFAULT_NOW,
    age_seconds: int = 1,
    fetch_age_seconds: int = 0,
    source_event_age_seconds: int | None = None,
    source: str = QUOTE_SOURCE_LATEST_QUOTE_IEX,
) -> IexLatestQuote:
    bid_price = bid if isinstance(bid, Decimal) else Decimal(bid)
    ask_price = ask if isinstance(ask, Decimal) else Decimal(ask)
    bid_qty = bid_size if isinstance(bid_size, Decimal) else Decimal(bid_size)
    ask_qty = ask_size if isinstance(ask_size, Decimal) else Decimal(ask_size)
    source_age = age_seconds if source_event_age_seconds is None else source_event_age_seconds
    return IexLatestQuote(
        symbol=symbol,
        bid_price=bid_price,
        ask_price=ask_price,
        bid_size=bid_qty,
        ask_size=ask_qty,
        source_timestamp=now - timedelta(seconds=source_age),
        fetched_at=now - timedelta(seconds=fetch_age_seconds),
        source=source,
    )


def universe_iex_quotes(
    *,
    now: datetime = DEFAULT_NOW,
    age_seconds: int = 1,
    fetch_age_seconds: int = 0,
    source_event_age_seconds: int | None = None,
    sgov_bid: Decimal | str | None = None,
    sgov_ask: Decimal | str | None = None,
) -> dict[str, IexLatestQuote]:
    bid = DEFAULT_PRICES["SGOV"] if sgov_bid is None else sgov_bid
    ask = Decimal("100.70") if sgov_ask is None else sgov_ask
    return {
        "SGOV": iex_quote(
            "SGOV",
            bid=bid,
            ask=ask,
            now=now,
            age_seconds=age_seconds,
            fetch_age_seconds=fetch_age_seconds,
            source_event_age_seconds=source_event_age_seconds,
        ),
        "BIL": iex_quote(
            "BIL",
            bid=DEFAULT_PRICES["BIL"],
            ask=Decimal("92.01"),
            now=now,
            age_seconds=age_seconds,
            fetch_age_seconds=fetch_age_seconds,
            source_event_age_seconds=source_event_age_seconds,
        ),
        "SHV": iex_quote(
            "SHV",
            bid=DEFAULT_PRICES["SHV"],
            ask=Decimal("110.01"),
            now=now,
            age_seconds=age_seconds,
            fetch_age_seconds=fetch_age_seconds,
            source_event_age_seconds=source_event_age_seconds,
        ),
    }


def iex_quote_matching_bound(bound: BoundExecutionPrice) -> IexLatestQuote:
    price = bound.quote.price
    return IexLatestQuote(
        symbol=bound.quote.symbol,
        bid_price=price,
        ask_price=price,
        bid_size=Decimal("1"),
        ask_size=Decimal("1"),
        source_timestamp=bound.quote.source_timestamp,
        fetched_at=bound.quote.fetched_at,
        source=QUOTE_SOURCE_LATEST_QUOTE_IEX,
    )


def market_data_from_bindings(
    bindings: Mapping[str, BoundExecutionPrice],
) -> FakeMarketData:
    quotes = {symbol: iex_quote_matching_bound(bound) for symbol, bound in bindings.items()}
    return FakeMarketData(quotes=quotes)
