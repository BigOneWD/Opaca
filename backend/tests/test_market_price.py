"""Fresh canonical quote validation. No synthetic fallback."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from opaca.domain.models import Side
from opaca.market.errors import (
    FutureQuoteError,
    MarketDataUnavailableError,
    QuoteValidationError,
    StaleQuoteError,
)
from opaca.market.quote import (
    DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS,
    QUOTE_SOURCE_LATEST_QUOTE_IEX,
    CanonicalMarketPrice,
    executable_canonical_price,
    quote_source_event_age_seconds,
    validate_canonical_quote,
)
from opaca.market.source import AlpacaPaperMarketData, FakeMarketData

from tests.helpers import DEFAULT_NOW
from tests.market_helpers import canonical_quote, iex_quote, universe_iex_quotes

PRODUCTION_ROOT = Path(__file__).resolve().parents[1] / "opaca"


class TestFreshQuote:
    def test_fresh_quote_accepted(self) -> None:
        quote = canonical_quote(age_seconds=1)
        assert validate_canonical_quote(quote, now=DEFAULT_NOW) is quote
        assert quote.price == Decimal("100.69")
        source = FakeMarketData(quotes=universe_iex_quotes())
        fetched = source.get_latest_quote("SGOV")
        canonical = executable_canonical_price(fetched, Side.BUY)
        validate_canonical_quote(canonical, now=DEFAULT_NOW)

    def test_stale_fetch_rejected(self) -> None:
        quote = canonical_quote(fetch_age_seconds=DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS + 1)
        with pytest.raises(StaleQuoteError, match="fetch age"):
            validate_canonical_quote(quote, now=DEFAULT_NOW)

    def test_source_event_age_alone_does_not_block(self) -> None:
        quote = canonical_quote(source_event_age_seconds=226, fetch_age_seconds=0)
        assert validate_canonical_quote(quote, now=DEFAULT_NOW) is quote
        assert quote_source_event_age_seconds(quote, now=DEFAULT_NOW) == 226

    def test_inclusive_fifteen_second_fetch_boundary(self) -> None:
        at_bound = CanonicalMarketPrice(
            symbol="SGOV",
            price=Decimal("100.69"),
            source_timestamp=DEFAULT_NOW - timedelta(seconds=1),
            fetched_at=DEFAULT_NOW - timedelta(seconds=15),
            source=QUOTE_SOURCE_LATEST_QUOTE_IEX,
        )
        assert validate_canonical_quote(at_bound, now=DEFAULT_NOW) is at_bound
        just_under = CanonicalMarketPrice(
            symbol="SGOV",
            price=Decimal("100.69"),
            source_timestamp=DEFAULT_NOW - timedelta(seconds=1),
            fetched_at=DEFAULT_NOW - timedelta(seconds=14, microseconds=999000),
            source=QUOTE_SOURCE_LATEST_QUOTE_IEX,
        )
        assert validate_canonical_quote(just_under, now=DEFAULT_NOW) is just_under
        beyond = CanonicalMarketPrice(
            symbol="SGOV",
            price=Decimal("100.69"),
            source_timestamp=DEFAULT_NOW - timedelta(seconds=1),
            fetched_at=DEFAULT_NOW - timedelta(seconds=15, microseconds=1),
            source=QUOTE_SOURCE_LATEST_QUOTE_IEX,
        )
        with pytest.raises(StaleQuoteError, match="fetch age"):
            validate_canonical_quote(beyond, now=DEFAULT_NOW)

    def test_future_quote_rejected(self) -> None:
        quote = CanonicalMarketPrice(
            symbol="SGOV",
            price=Decimal("100.69"),
            source_timestamp=DEFAULT_NOW + timedelta(seconds=5),
            fetched_at=DEFAULT_NOW,
            source=QUOTE_SOURCE_LATEST_QUOTE_IEX,
        )
        with pytest.raises(FutureQuoteError, match="source_timestamp"):
            validate_canonical_quote(quote, now=DEFAULT_NOW)
        future_fetch = CanonicalMarketPrice(
            symbol="SGOV",
            price=Decimal("100.69"),
            source_timestamp=DEFAULT_NOW - timedelta(seconds=1),
            fetched_at=DEFAULT_NOW + timedelta(seconds=5),
            source=QUOTE_SOURCE_LATEST_QUOTE_IEX,
        )
        with pytest.raises(FutureQuoteError, match="fetched_at"):
            validate_canonical_quote(future_fetch, now=DEFAULT_NOW)

    @pytest.mark.parametrize(
        "bad",
        [
            Decimal("0"),
            Decimal("-1.00"),
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
        ],
    )
    def test_malformed_price_rejected_at_construction(self, bad: Decimal) -> None:
        with pytest.raises(QuoteValidationError):
            CanonicalMarketPrice(
                symbol="SGOV",
                price=bad,
                source_timestamp=DEFAULT_NOW,
                fetched_at=DEFAULT_NOW,
                source=QUOTE_SOURCE_LATEST_QUOTE_IEX,
            )

    def test_unknown_symbol_rejected(self) -> None:
        with pytest.raises(QuoteValidationError, match="permitted universe"):
            canonical_quote(symbol="AAPL")

    def test_missing_market_data_rejected(self) -> None:
        source = FakeMarketData(quotes={})
        with pytest.raises(MarketDataUnavailableError, match="no latest quote"):
            source.get_latest_quote("SGOV")
        source = FakeMarketData(quotes=universe_iex_quotes(), unavailable=True)
        with pytest.raises(MarketDataUnavailableError, match="unavailable"):
            source.get_latest_quote("SGOV")


class _QuoteClient:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def get_stock_latest_quote(self, request: object = None) -> object:
        del request
        return self.payload


def _sgov_quote_payload(
    *,
    bid: object = "100.69",
    ask: object = "100.70",
    bid_size: object = "1",
    ask_size: object = "1",
    timestamp: object | None = None,
    symbol: object | None = None,
    feed: object | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "bid_price": bid,
        "ask_price": ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "timestamp": DEFAULT_NOW.isoformat() if timestamp is None else timestamp,
    }
    if symbol is not None:
        body["symbol"] = symbol
    if feed is not None:
        body["feed"] = feed
    return {"SGOV": body}


class TestAlpacaAdapterFailClosed:
    def test_zero_price_rejected(self) -> None:
        client = _QuoteClient(_sgov_quote_payload(bid="0", ask="100.70"))
        with pytest.raises(QuoteValidationError):
            AlpacaPaperMarketData(client).get_latest_quote("SGOV")

    def test_negative_price_rejected(self) -> None:
        client = _QuoteClient(_sgov_quote_payload(bid="-1.00", ask="100.70"))
        with pytest.raises(QuoteValidationError):
            AlpacaPaperMarketData(client).get_latest_quote("SGOV")

    def test_nan_price_rejected(self) -> None:
        client = _QuoteClient(_sgov_quote_payload(bid=float("nan"), ask="100.70"))
        with pytest.raises(QuoteValidationError):
            AlpacaPaperMarketData(client).get_latest_quote("SGOV")

    def test_missing_payload_rejected(self) -> None:
        client = _QuoteClient({})
        with pytest.raises(MarketDataUnavailableError):
            AlpacaPaperMarketData(client).get_latest_quote("SGOV")

    def test_fresh_sdk_quote_accepted(self) -> None:
        client = _QuoteClient(_sgov_quote_payload())
        quote = AlpacaPaperMarketData(client).get_latest_quote("SGOV")
        assert quote.bid_price == Decimal("100.69")
        assert quote.ask_price == Decimal("100.70")
        assert quote.source == QUOTE_SOURCE_LATEST_QUOTE_IEX
        assert quote.symbol == "SGOV"
        canonical = executable_canonical_price(quote, Side.BUY)
        assert canonical.price == Decimal("100.70")

    def test_iex_feed_is_requested(self) -> None:
        from alpaca.data.enums import DataFeed

        captured: list[object] = []

        class _CaptureClient:
            def get_stock_latest_quote(self, request: object = None) -> object:
                captured.append(request)
                return _sgov_quote_payload()

        quote = AlpacaPaperMarketData(_CaptureClient()).get_latest_quote("SGOV")
        assert quote.ask_price == Decimal("100.70")
        assert len(captured) == 1
        feed = getattr(captured[0], "feed", None)
        assert feed is DataFeed.IEX
        assert getattr(captured[0], "symbol_or_symbols", None) == "SGOV"
        assert type(captured[0]).__name__ == "StockLatestQuoteRequest"

    def test_adapter_has_no_latest_trade_callable(self) -> None:
        client = _QuoteClient(_sgov_quote_payload())
        adapter = AlpacaPaperMarketData(client)
        assert not hasattr(adapter, "get_latest_trade")
        assert not hasattr(adapter, "_get_stock_latest_trade")


def test_production_live_paper_path_does_not_use_fixture_prices() -> None:
    for path in PRODUCTION_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "DEFAULT_PRICES" not in text
        assert "tests.helpers" not in text


def test_iex_quote_helper_roundtrip() -> None:
    quote = iex_quote()
    assert quote.source == QUOTE_SOURCE_LATEST_QUOTE_IEX
    assert quote.bid_price < quote.ask_price


class TestDualFreshnessSemantics:
    def test_fetched_now_source_event_5s_old_passes(self) -> None:
        quote = canonical_quote(source_event_age_seconds=5, fetch_age_seconds=0)
        assert validate_canonical_quote(quote, now=DEFAULT_NOW) is quote

    def test_fetched_now_source_event_96s_old_passes(self) -> None:
        quote = canonical_quote(source_event_age_seconds=96, fetch_age_seconds=0)
        assert validate_canonical_quote(quote, now=DEFAULT_NOW) is quote
        iex = iex_quote(source_event_age_seconds=96, fetch_age_seconds=0)
        canonical = executable_canonical_price(iex, Side.BUY)
        validate_canonical_quote(canonical, now=DEFAULT_NOW)

    def test_fetched_now_source_event_226s_old_passes(self) -> None:
        quote = CanonicalMarketPrice(
            symbol="SGOV",
            price=Decimal("100.69"),
            source_timestamp=DEFAULT_NOW - timedelta(seconds=226),
            fetched_at=DEFAULT_NOW,
            source=QUOTE_SOURCE_LATEST_QUOTE_IEX,
        )
        assert validate_canonical_quote(quote, now=DEFAULT_NOW) is quote
        assert quote_source_event_age_seconds(quote, now=DEFAULT_NOW) == 226

    def test_fetched_now_source_event_10_minutes_old_passes(self) -> None:
        quote = CanonicalMarketPrice(
            symbol="SGOV",
            price=Decimal("100.69"),
            source_timestamp=DEFAULT_NOW - timedelta(minutes=10),
            fetched_at=DEFAULT_NOW,
            source=QUOTE_SOURCE_LATEST_QUOTE_IEX,
        )
        assert validate_canonical_quote(quote, now=DEFAULT_NOW) is quote

    def test_fetched_15s_ago_passes(self) -> None:
        quote = CanonicalMarketPrice(
            symbol="SGOV",
            price=Decimal("100.69"),
            source_timestamp=DEFAULT_NOW - timedelta(seconds=15),
            fetched_at=DEFAULT_NOW - timedelta(seconds=15),
            source=QUOTE_SOURCE_LATEST_QUOTE_IEX,
        )
        assert validate_canonical_quote(quote, now=DEFAULT_NOW) is quote

    def test_fetched_15s_plus_1us_blocked(self) -> None:
        quote = CanonicalMarketPrice(
            symbol="SGOV",
            price=Decimal("100.69"),
            source_timestamp=DEFAULT_NOW - timedelta(seconds=15, microseconds=1),
            fetched_at=DEFAULT_NOW - timedelta(seconds=15, microseconds=1),
            source=QUOTE_SOURCE_LATEST_QUOTE_IEX,
        )
        with pytest.raises(StaleQuoteError, match="fetch age"):
            validate_canonical_quote(quote, now=DEFAULT_NOW)

    def test_fetched_timestamp_future_blocked(self) -> None:
        quote = CanonicalMarketPrice(
            symbol="SGOV",
            price=Decimal("100.69"),
            source_timestamp=DEFAULT_NOW,
            fetched_at=DEFAULT_NOW + timedelta(microseconds=1),
            source=QUOTE_SOURCE_LATEST_QUOTE_IEX,
        )
        with pytest.raises(FutureQuoteError, match="fetched_at"):
            validate_canonical_quote(quote, now=DEFAULT_NOW)

    def test_source_timestamp_future_blocked(self) -> None:
        quote = CanonicalMarketPrice(
            symbol="SGOV",
            price=Decimal("100.69"),
            source_timestamp=DEFAULT_NOW + timedelta(microseconds=1),
            fetched_at=DEFAULT_NOW,
            source=QUOTE_SOURCE_LATEST_QUOTE_IEX,
        )
        with pytest.raises(FutureQuoteError, match="source_timestamp"):
            validate_canonical_quote(quote, now=DEFAULT_NOW)

    def test_fresh_transport_old_source_event_passes(self) -> None:
        quote = canonical_quote(
            fetch_age_seconds=0,
            source_event_age_seconds=226,
        )
        assert validate_canonical_quote(quote, now=DEFAULT_NOW) is quote

    def test_stale_transport_fresh_source_blocked(self) -> None:
        quote = CanonicalMarketPrice(
            symbol="SGOV",
            price=Decimal("100.69"),
            source_timestamp=DEFAULT_NOW - timedelta(seconds=1),
            fetched_at=DEFAULT_NOW - timedelta(seconds=DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS + 1),
            source=QUOTE_SOURCE_LATEST_QUOTE_IEX,
        )
        with pytest.raises(StaleQuoteError, match="fetch age"):
            validate_canonical_quote(quote, now=DEFAULT_NOW)
