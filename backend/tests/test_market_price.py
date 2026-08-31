"""Fresh canonical quote validation. No synthetic fallback."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from opaca.market.errors import (
    FutureQuoteError,
    MarketDataUnavailableError,
    QuoteValidationError,
    StaleQuoteError,
)
from opaca.market.quote import (
    DEFAULT_MAX_QUOTE_AGE_SECONDS,
    CanonicalMarketPrice,
    validate_canonical_quote,
)
from opaca.market.source import AlpacaPaperMarketData, FakeMarketData

from tests.helpers import DEFAULT_NOW
from tests.market_helpers import canonical_quote, universe_quotes

PRODUCTION_ROOT = Path(__file__).resolve().parents[1] / "opaca"


class TestFreshQuote:
    def test_fresh_quote_accepted(self) -> None:
        quote = canonical_quote(age_seconds=1)
        assert validate_canonical_quote(quote, now=DEFAULT_NOW) is quote
        assert quote.price == Decimal("100.69")
        source = FakeMarketData(quotes=universe_quotes())
        fetched = source.get_latest_trade("SGOV")
        validate_canonical_quote(fetched, now=DEFAULT_NOW)

    def test_stale_quote_rejected(self) -> None:
        quote = canonical_quote(age_seconds=DEFAULT_MAX_QUOTE_AGE_SECONDS + 1)
        with pytest.raises(StaleQuoteError, match="fail closed"):
            validate_canonical_quote(quote, now=DEFAULT_NOW)

    def test_inclusive_fifteen_second_boundary(self) -> None:
        at_bound = CanonicalMarketPrice(
            symbol="SGOV",
            price=Decimal("100.69"),
            source_timestamp=DEFAULT_NOW - timedelta(seconds=15),
            fetched_at=DEFAULT_NOW,
            source="alpaca.stock.latest_trade",
        )
        assert validate_canonical_quote(at_bound, now=DEFAULT_NOW) is at_bound
        just_under = CanonicalMarketPrice(
            symbol="SGOV",
            price=Decimal("100.69"),
            source_timestamp=DEFAULT_NOW - timedelta(seconds=14, microseconds=999000),
            fetched_at=DEFAULT_NOW,
            source="alpaca.stock.latest_trade",
        )
        assert validate_canonical_quote(just_under, now=DEFAULT_NOW) is just_under
        beyond = CanonicalMarketPrice(
            symbol="SGOV",
            price=Decimal("100.69"),
            source_timestamp=DEFAULT_NOW - timedelta(seconds=15, microseconds=1),
            fetched_at=DEFAULT_NOW,
            source="alpaca.stock.latest_trade",
        )
        with pytest.raises(StaleQuoteError, match="fail closed"):
            validate_canonical_quote(beyond, now=DEFAULT_NOW)

    def test_future_quote_rejected(self) -> None:
        quote = CanonicalMarketPrice(
            symbol="SGOV",
            price=Decimal("100.69"),
            source_timestamp=DEFAULT_NOW + timedelta(seconds=5),
            fetched_at=DEFAULT_NOW,
            source="alpaca.stock.latest_trade",
        )
        with pytest.raises(FutureQuoteError, match="future"):
            validate_canonical_quote(quote, now=DEFAULT_NOW)

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
                source="alpaca.stock.latest_trade",
            )

    def test_unknown_symbol_rejected(self) -> None:
        with pytest.raises(QuoteValidationError, match="permitted universe"):
            canonical_quote(symbol="AAPL")

    def test_missing_market_data_rejected(self) -> None:
        source = FakeMarketData(quotes={})
        with pytest.raises(MarketDataUnavailableError, match="no latest trade"):
            source.get_latest_trade("SGOV")
        source = FakeMarketData(quotes=universe_quotes(), unavailable=True)
        with pytest.raises(MarketDataUnavailableError, match="unavailable"):
            source.get_latest_trade("SGOV")


class _TradeClient:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def get_stock_latest_trade(self, request: object = None) -> object:
        del request
        return self.payload


class TestAlpacaAdapterFailClosed:
    def test_zero_price_rejected(self) -> None:
        client = _TradeClient({"SGOV": {"price": "0", "timestamp": DEFAULT_NOW.isoformat()}})
        with pytest.raises(QuoteValidationError):
            AlpacaPaperMarketData(client).get_latest_trade("SGOV")

    def test_negative_price_rejected(self) -> None:
        client = _TradeClient({"SGOV": {"price": "-1.00", "timestamp": DEFAULT_NOW.isoformat()}})
        with pytest.raises(QuoteValidationError):
            AlpacaPaperMarketData(client).get_latest_trade("SGOV")

    def test_nan_price_rejected(self) -> None:
        client = _TradeClient(
            {"SGOV": {"price": float("nan"), "timestamp": DEFAULT_NOW.isoformat()}}
        )
        with pytest.raises(QuoteValidationError):
            AlpacaPaperMarketData(client).get_latest_trade("SGOV")

    def test_missing_payload_rejected(self) -> None:
        client = _TradeClient({})
        with pytest.raises(MarketDataUnavailableError):
            AlpacaPaperMarketData(client).get_latest_trade("SGOV")

    def test_fresh_sdk_trade_accepted(self) -> None:
        client = _TradeClient({"SGOV": {"price": "100.69", "timestamp": DEFAULT_NOW.isoformat()}})
        quote = AlpacaPaperMarketData(client).get_latest_trade("SGOV")
        assert quote.price == Decimal("100.69")
        assert quote.source == "alpaca.stock.latest_trade"
        assert quote.symbol == "SGOV"

    def test_iex_feed_is_requested(self) -> None:
        from alpaca.data.enums import DataFeed

        captured: list[object] = []

        class _CaptureClient:
            def get_stock_latest_trade(self, request: object = None) -> object:
                captured.append(request)
                return {"SGOV": {"price": "100.69", "timestamp": DEFAULT_NOW.isoformat()}}

        quote = AlpacaPaperMarketData(_CaptureClient()).get_latest_trade("SGOV")
        assert quote.price == Decimal("100.69")
        assert len(captured) == 1
        feed = getattr(captured[0], "feed", None)
        assert feed is DataFeed.IEX
        assert getattr(captured[0], "symbol_or_symbols", None) == "SGOV"


def test_production_live_paper_path_does_not_use_fixture_prices() -> None:
    for path in PRODUCTION_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "DEFAULT_PRICES" not in text
        assert "tests.helpers" not in text
