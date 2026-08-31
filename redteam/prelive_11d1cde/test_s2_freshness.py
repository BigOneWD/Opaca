"""S2: price validation / freshness. Every unsafe input must fail closed."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

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
from opaca.market.source import AlpacaPaperMarketData

from support import DEFAULT_NOW, quote


def reader(payload):
    class R:
        def get_stock_latest_trade(self, request):
            if isinstance(payload, Exception):
                raise payload
            return payload
    return AlpacaPaperMarketData(R())


def test_default_max_age_is_fifteen_seconds():
    assert DEFAULT_MAX_QUOTE_AGE_SECONDS == 15


# ------------------------------------------------------------------ missing


def test_missing_trade_fails_closed():
    for payload in (None, {}, {"SGOV": None}, {"BIL": {"price": "92", "timestamp": "x"}}):
        with pytest.raises(MarketDataUnavailableError):
            reader(payload).get_latest_trade("SGOV")


def test_missing_timestamp_fails_closed():
    with pytest.raises((QuoteValidationError, MarketDataUnavailableError)):
        reader({"SGOV": {"price": "100.69"}}).get_latest_trade("SGOV")
    with pytest.raises((QuoteValidationError, MarketDataUnavailableError)):
        reader({"SGOV": {"price": "100.69", "timestamp": None}}).get_latest_trade("SGOV")


def test_missing_price_fails_closed():
    with pytest.raises((QuoteValidationError, MarketDataUnavailableError)):
        reader({"SGOV": {"timestamp": "2026-09-01T14:29:59Z"}}).get_latest_trade("SGOV")


# ------------------------------------------------------------------ freshness


def test_a_stale_quote_is_refused():
    with pytest.raises(StaleQuoteError):
        validate_canonical_quote(quote(age_seconds=60), now=DEFAULT_NOW)


def test_exact_fifteen_second_boundary_is_accepted_and_just_beyond_is_not():
    at_bound = CanonicalMarketPrice(
        symbol="SGOV", price=Decimal("100.69"),
        source_timestamp=DEFAULT_NOW - timedelta(seconds=15),
        fetched_at=DEFAULT_NOW, source="alpaca.stock.latest_trade")
    assert validate_canonical_quote(at_bound, now=DEFAULT_NOW) is at_bound

    beyond = CanonicalMarketPrice(
        symbol="SGOV", price=Decimal("100.69"),
        source_timestamp=DEFAULT_NOW - timedelta(seconds=15, microseconds=1),
        fetched_at=DEFAULT_NOW, source="alpaca.stock.latest_trade")
    with pytest.raises(StaleQuoteError):
        validate_canonical_quote(beyond, now=DEFAULT_NOW)


def test_a_future_timestamp_fails_closed():
    with pytest.raises(FutureQuoteError):
        validate_canonical_quote(quote(age_seconds=-1), now=DEFAULT_NOW)
    with pytest.raises(FutureQuoteError):
        validate_canonical_quote(quote(age_seconds=-86400), now=DEFAULT_NOW)


def test_a_naive_source_timestamp_cannot_be_constructed():
    with pytest.raises(QuoteValidationError):
        CanonicalMarketPrice(
            symbol="SGOV", price=Decimal("100.69"),
            source_timestamp=datetime(2026, 9, 1, 14, 29, 59),
            fetched_at=DEFAULT_NOW, source="alpaca.stock.latest_trade")


def test_a_naive_evaluation_now_fails_closed():
    with pytest.raises(QuoteValidationError):
        validate_canonical_quote(quote(), now=datetime(2026, 9, 1, 14, 30))


def test_a_non_utc_but_valid_offset_is_compared_correctly():
    tz = timezone(timedelta(hours=8))
    q = CanonicalMarketPrice(
        symbol="SGOV", price=Decimal("100.69"),
        source_timestamp=(DEFAULT_NOW - timedelta(seconds=5)).astimezone(tz),
        fetched_at=DEFAULT_NOW, source="alpaca.stock.latest_trade")
    assert validate_canonical_quote(q, now=DEFAULT_NOW) is q
    stale = CanonicalMarketPrice(
        symbol="SGOV", price=Decimal("100.69"),
        source_timestamp=(DEFAULT_NOW - timedelta(seconds=45)).astimezone(tz),
        fetched_at=DEFAULT_NOW, source="alpaca.stock.latest_trade")
    with pytest.raises(StaleQuoteError):
        validate_canonical_quote(stale, now=DEFAULT_NOW)


@pytest.mark.parametrize("raw", [
    "not-a-timestamp", "2026-13-45T99:99:99Z", "", 0, -1, 1.5, [], {},
    "2026-09-01 14:29:59",
])
def test_malformed_timestamps_fail_closed(raw):
    with pytest.raises((QuoteValidationError, MarketDataUnavailableError)):
        reader({"SGOV": {"price": "100.69", "timestamp": raw}}).get_latest_trade("SGOV")


def test_a_naive_timestamp_string_from_the_sdk_fails_closed():
    with pytest.raises((QuoteValidationError, MarketDataUnavailableError)):
        reader({"SGOV": {"price": "100.69",
                         "timestamp": datetime(2026, 9, 1, 14, 29, 59)}}
               ).get_latest_trade("SGOV")


# ------------------------------------------------------------------ magnitude


@pytest.mark.parametrize("raw", [
    "0", "-1", "-100.69", "0.00", "nan", "NaN", "inf", "-inf", "Infinity",
    "", "abc", None, True, False, [], {}, object(),
])
def test_malformed_prices_fail_closed(raw):
    with pytest.raises((QuoteValidationError, MarketDataUnavailableError)):
        reader({"SGOV": {"price": raw, "timestamp": "2026-09-01T14:29:59Z"}}
               ).get_latest_trade("SGOV")


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf"), 0.0, -1.0])
def test_non_finite_or_non_positive_floats_fail_closed(raw):
    with pytest.raises((QuoteValidationError, MarketDataUnavailableError)):
        reader({"SGOV": {"price": raw, "timestamp": "2026-09-01T14:29:59Z"}}
               ).get_latest_trade("SGOV")


@pytest.mark.parametrize("raw", [
    Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"),
    Decimal("0"), Decimal("-3"), Decimal("1e26"), Decimal("1e40"),
])
def test_non_finite_decimals_fail_closed(raw):
    with pytest.raises((QuoteValidationError, MarketDataUnavailableError)):
        reader({"SGOV": {"price": raw, "timestamp": "2026-09-01T14:29:59Z"}}
               ).get_latest_trade("SGOV")


@pytest.mark.parametrize("raw", [
    Decimal("NaN"), Decimal("Infinity"), Decimal("0"), Decimal("-1"), Decimal("1e26"),
])
def test_the_canonical_price_object_itself_refuses_bad_prices(raw):
    with pytest.raises(QuoteValidationError):
        CanonicalMarketPrice(
            symbol="SGOV", price=raw,
            source_timestamp=DEFAULT_NOW - timedelta(seconds=1),
            fetched_at=DEFAULT_NOW, source="alpaca.stock.latest_trade")


def test_an_empty_source_string_is_refused():
    with pytest.raises(QuoteValidationError):
        CanonicalMarketPrice(
            symbol="SGOV", price=Decimal("100.69"),
            source_timestamp=DEFAULT_NOW - timedelta(seconds=1),
            fetched_at=DEFAULT_NOW, source="")


def test_a_zero_or_negative_max_age_is_refused_not_treated_as_infinite():
    for bad in (0, -1, -15):
        with pytest.raises(QuoteValidationError):
            validate_canonical_quote(quote(), now=DEFAULT_NOW, max_age_seconds=bad)


def test_broker_side_exception_never_degrades_to_a_price():
    with pytest.raises(MarketDataUnavailableError):
        reader(RuntimeError("500 upstream")).get_latest_trade("SGOV")
    with pytest.raises(MarketDataUnavailableError):
        reader(TimeoutError("read timeout")).get_latest_trade("SGOV")


def test_a_valid_sdk_float_is_never_used_as_a_float():
    got = reader({"SGOV": {"price": 100.69, "timestamp": "2026-09-01T14:29:59Z"}}
                 ).get_latest_trade("SGOV")
    assert isinstance(got.price, Decimal)
    assert got.price == Decimal("100.69000000")
    assert got.fetched_at.tzinfo is not None
    assert got.fetched_at <= datetime.now(UTC)
