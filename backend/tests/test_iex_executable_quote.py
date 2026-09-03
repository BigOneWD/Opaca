"""IEX latest-quote executable pricing. No last-trade or SIP fallback."""

from __future__ import annotations

import ast
from datetime import timedelta
from decimal import ROUND_UP, Decimal
from pathlib import Path

import pytest
from opaca.domain.models import Position, Side
from opaca.domain.money import CENT
from opaca.market.binding import bind_buy, bind_sell
from opaca.market.errors import (
    FutureQuoteError,
    MarketDataUnavailableError,
    QuoteValidationError,
)
from opaca.market.limit import DEFAULT_BUY_LIMIT_TOLERANCE, max_buy_cash_obligation
from opaca.market.quote import (
    DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS,
    QUOTE_SOURCE_LATEST_QUOTE_IEX,
    CanonicalMarketPrice,
    canonical_prices_for_decision,
    executable_canonical_price,
    required_pricing_symbols,
    validate_canonical_quote,
)
from opaca.market.source import AlpacaPaperMarketData, FakeMarketData, latest_quotes
from opaca.persistence.demo import PAPER_DEMO_DB_NAME
from opaca.preflight import EXECUTION_NOT_ATTEMPTED, run_read_only_preflight

from tests.helpers import DEFAULT_NOW, DEFAULT_PRICES
from tests.market_helpers import iex_quote
from tests.state_helpers import paper_gateway, position_payload

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOT = BACKEND_ROOT / "opaca"
LIVE_MUTATION_TEST = BACKEND_ROOT / "tests" / "test_live_paper_mutation.py"


def _held(symbol: str, qty: str = "1") -> Position:
    quantity = Decimal(qty)
    return Position(
        symbol=symbol,
        quantity=quantity,
        quantity_available=quantity,
        market_value=quantity,
    )


def _payload(
    *,
    bid: object = "100.69",
    ask: object = "100.70",
    bid_size: object = "1",
    ask_size: object = "1",
    timestamp: object | None = None,
    symbol: object | None = None,
    feed: object | None = None,
    key: str = "SGOV",
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
    return {key: body}


class _QuoteClient:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def get_stock_latest_quote(self, request: object = None) -> object:
        del request
        return self.payload


class TestBuyUsesAsk:
    def test_buy_uses_ask_never_bid_or_last_trade(self) -> None:
        quote = iex_quote(bid="100.69", ask="100.70")
        canonical = executable_canonical_price(quote, Side.BUY)
        assert canonical.price == Decimal("100.70")
        assert canonical.price != quote.bid_price
        assert canonical.source == QUOTE_SOURCE_LATEST_QUOTE_IEX
        bound = bind_buy(canonical, Decimal("1"))
        assert bound.valuation_price == Decimal("100.70")


class TestSellUsesBid:
    def test_sell_uses_bid(self) -> None:
        quote = iex_quote(bid="100.69", ask="100.70")
        canonical = executable_canonical_price(quote, Side.SELL)
        assert canonical.price == Decimal("100.69")
        assert canonical.price != quote.ask_price
        bound = bind_sell(canonical, Decimal("1"))
        assert bound.valuation_price == Decimal("100.69")


class TestExactIexFeed:
    def test_exact_iex_feed_requested(self) -> None:
        from alpaca.data.enums import DataFeed

        captured: list[object] = []

        class _Capture:
            def get_stock_latest_quote(self, request: object = None) -> object:
                captured.append(request)
                return _payload()

        AlpacaPaperMarketData(_Capture()).get_latest_quote("SGOV")
        assert getattr(captured[0], "feed", None) is DataFeed.IEX
        assert type(captured[0]).__name__ == "StockLatestQuoteRequest"


class TestRequiredSymbols:
    def test_sgov_required_unused_stale_whitelist_does_not_block(self, tmp_path: Path) -> None:
        quotes = {
            "SGOV": iex_quote("SGOV", bid="100.69", ask="100.70", age_seconds=1),
            "BIL": iex_quote(
                "BIL",
                bid="91.66",
                ask="91.67",
                source_event_age_seconds=226,
            ),
            "SHV": iex_quote(
                "SHV",
                bid="110.37",
                ask="110.38",
                source_event_age_seconds=226,
            ),
        }
        required = required_pricing_symbols(("SGOV",), ())
        assert required == ("SGOV",)
        fetched = latest_quotes(FakeMarketData(quotes=quotes), required)
        canonical = canonical_prices_for_decision(fetched, side_by_symbol={"SGOV": Side.BUY})
        validate_canonical_quote(canonical["SGOV"], now=DEFAULT_NOW)
        aged = {
            "SGOV": iex_quote("SGOV", bid="100.69", ask="100.70", source_event_age_seconds=96),
            "BIL": quotes["BIL"],
            "SHV": quotes["SHV"],
        }
        report_96 = run_read_only_preflight(
            paper_gateway(),
            FakeMarketData(quotes=aged),
            now=DEFAULT_NOW,
            db_path=tmp_path / "sgov-96.db",
        )
        assert report_96.fail_reason is None
        assert report_96.source_event_age_seconds == 96
        assert report_96.fetch_age_seconds == 0
        report = run_read_only_preflight(
            paper_gateway(),
            FakeMarketData(quotes=quotes),
            now=DEFAULT_NOW,
            db_path=tmp_path / PAPER_DEMO_DB_NAME,
        )
        assert report.treasuryguard == "PASS"
        assert report.execution == EXECUTION_NOT_ATTEMPTED
        assert report.quote_symbol == "SGOV"

    def test_held_bil_plus_sgov_proposal_makes_bil_required(self) -> None:
        required = required_pricing_symbols(("SGOV",), (_held("BIL"),))
        assert required == ("BIL", "SGOV")

    def test_required_sgov_old_source_event_passes(self, tmp_path: Path) -> None:
        quotes = {
            "SGOV": iex_quote("SGOV", source_event_age_seconds=226, fetch_age_seconds=0),
            "BIL": iex_quote("BIL", bid="91.66", ask="91.67", age_seconds=1),
            "SHV": iex_quote("SHV", bid="110.37", ask="110.38", age_seconds=1),
        }
        report = run_read_only_preflight(
            paper_gateway(),
            FakeMarketData(quotes=quotes),
            now=DEFAULT_NOW,
            db_path=tmp_path / PAPER_DEMO_DB_NAME,
        )
        assert report.fail_reason is None
        assert report.treasuryguard == "PASS"
        assert report.execution == EXECUTION_NOT_ATTEMPTED
        assert report.source_event_age_seconds == 226
        assert report.fetch_age_seconds == 0
        assert "(diagnostic)" in report.render()

    def test_stale_required_sgov_fetch_blocked(self, tmp_path: Path) -> None:
        quotes = {
            "SGOV": iex_quote(
                "SGOV",
                fetch_age_seconds=DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS + 1,
                source_event_age_seconds=DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS + 1,
            ),
            "BIL": iex_quote("BIL", bid="91.66", ask="91.67", age_seconds=1),
            "SHV": iex_quote("SHV", bid="110.37", ask="110.38", age_seconds=1),
        }
        report = run_read_only_preflight(
            paper_gateway(),
            FakeMarketData(quotes=quotes),
            now=DEFAULT_NOW,
            db_path=tmp_path / PAPER_DEMO_DB_NAME,
        )
        assert report.fail_reason is not None
        assert "fetch age" in report.fail_reason
        assert report.execution == EXECUTION_NOT_ATTEMPTED

    def test_stale_held_bil_fetch_blocked(self, tmp_path: Path) -> None:
        quotes = {
            "SGOV": iex_quote("SGOV", age_seconds=1),
            "BIL": iex_quote(
                "BIL",
                bid="91.66",
                ask="91.67",
                fetch_age_seconds=DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS + 1,
                source_event_age_seconds=DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS + 1,
            ),
            "SHV": iex_quote("SHV", bid="110.37", ask="110.38", age_seconds=1),
        }
        gateway = paper_gateway(
            positions=(position_payload(symbol="BIL", qty="1", price=DEFAULT_PRICES["BIL"]),)
        )
        report = run_read_only_preflight(
            gateway,
            FakeMarketData(quotes=quotes),
            now=DEFAULT_NOW,
            db_path=tmp_path / PAPER_DEMO_DB_NAME,
        )
        assert report.fail_reason is not None
        assert "fetch age" in report.fail_reason
        assert report.execution == EXECUTION_NOT_ATTEMPTED

    def test_held_bil_old_source_event_does_not_block(self, tmp_path: Path) -> None:
        quotes = {
            "SGOV": iex_quote("SGOV", age_seconds=1),
            "BIL": iex_quote(
                "BIL",
                bid="91.66",
                ask="91.67",
                source_event_age_seconds=226,
                fetch_age_seconds=0,
            ),
            "SHV": iex_quote("SHV", bid="110.37", ask="110.38", age_seconds=1),
        }
        gateway = paper_gateway(
            positions=(position_payload(symbol="BIL", qty="1", price=DEFAULT_PRICES["BIL"]),)
        )
        report = run_read_only_preflight(
            gateway,
            FakeMarketData(quotes=quotes),
            now=DEFAULT_NOW,
            db_path=tmp_path / PAPER_DEMO_DB_NAME,
        )
        assert report.fail_reason is None
        assert report.treasuryguard == "PASS"

    def test_stale_unused_bil_does_not_block(self, tmp_path: Path) -> None:
        quotes = {
            "SGOV": iex_quote("SGOV", age_seconds=1),
            "BIL": iex_quote(
                "BIL",
                bid="91.66",
                ask="91.67",
                source_event_age_seconds=226,
            ),
            "SHV": iex_quote("SHV", bid="110.37", ask="110.38", age_seconds=1),
        }
        report = run_read_only_preflight(
            paper_gateway(),
            FakeMarketData(quotes=quotes),
            now=DEFAULT_NOW,
            db_path=tmp_path / PAPER_DEMO_DB_NAME,
        )
        assert report.fail_reason is None
        assert report.treasuryguard == "PASS"

    def test_missing_required_quote_blocked(self) -> None:
        source = FakeMarketData(quotes={})
        with pytest.raises(MarketDataUnavailableError, match="no latest quote"):
            latest_quotes(source, ("SGOV",))


class TestQuoteValidation:
    def test_bid_greater_than_ask_blocked(self) -> None:
        with pytest.raises(QuoteValidationError, match="exceeds ask"):
            iex_quote(bid="100.80", ask="100.70")
        client = _QuoteClient(_payload(bid="100.80", ask="100.70"))
        with pytest.raises(QuoteValidationError, match="exceeds ask"):
            AlpacaPaperMarketData(client).get_latest_quote("SGOV")

    def test_zero_bid_or_ask_blocked(self) -> None:
        client = _QuoteClient(_payload(bid="0", ask="100.70"))
        with pytest.raises(QuoteValidationError):
            AlpacaPaperMarketData(client).get_latest_quote("SGOV")
        client = _QuoteClient(_payload(bid="100.69", ask="0"))
        with pytest.raises(QuoteValidationError):
            AlpacaPaperMarketData(client).get_latest_quote("SGOV")

    def test_zero_executable_side_size_blocked(self) -> None:
        buy = iex_quote(ask_size="0")
        with pytest.raises(QuoteValidationError, match="ask_size"):
            executable_canonical_price(buy, Side.BUY)
        sell = iex_quote(bid_size="0")
        with pytest.raises(QuoteValidationError, match="bid_size"):
            executable_canonical_price(sell, Side.SELL)
        client = _QuoteClient(_payload(ask_size="0"))
        quote = AlpacaPaperMarketData(client).get_latest_quote("SGOV")
        with pytest.raises(QuoteValidationError, match="ask_size"):
            executable_canonical_price(quote, Side.BUY)

    def test_future_quote_blocked(self) -> None:
        future_canonical = CanonicalMarketPrice(
            symbol="SGOV",
            price=Decimal("100.70"),
            source_timestamp=DEFAULT_NOW + timedelta(seconds=3),
            fetched_at=DEFAULT_NOW,
            source=QUOTE_SOURCE_LATEST_QUOTE_IEX,
        )
        with pytest.raises(FutureQuoteError):
            validate_canonical_quote(future_canonical, now=DEFAULT_NOW)

    def test_malformed_sdk_payload_blocked(self) -> None:
        client = _QuoteClient({"SGOV": {"timestamp": DEFAULT_NOW.isoformat()}})
        with pytest.raises(QuoteValidationError):
            AlpacaPaperMarketData(client).get_latest_quote("SGOV")
        client = _QuoteClient({"SGOV": object()})
        with pytest.raises((QuoteValidationError, MarketDataUnavailableError)):
            AlpacaPaperMarketData(client).get_latest_quote("SGOV")
        naive = _QuoteClient(_payload(timestamp="2026-08-31T15:32:08"))
        with pytest.raises(QuoteValidationError, match="naive"):
            AlpacaPaperMarketData(naive).get_latest_quote("SGOV")
        wrong = _QuoteClient(_payload(symbol="BIL"))
        with pytest.raises(QuoteValidationError, match="wrong symbol"):
            AlpacaPaperMarketData(wrong).get_latest_quote("SGOV")


class TestNoFallback:
    def test_no_latest_trade_fallback_in_production(self) -> None:
        needles = (
            "get_stock_latest_trade",
            "StockLatestTradeRequest",
            "latest_trades",
            "get_latest_trade",
            "latest_trade",
        )
        for path in PRODUCTION_ROOT.rglob("*.py"):
            if path == PRODUCTION_ROOT / "wheel" / "mcp_guard.py":
                continue
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                assert needle not in text, f"{path}: {needle}"

    def test_no_sip_fallback_in_production(self) -> None:
        for path in PRODUCTION_ROOT.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "DataFeed.SIP" not in text, str(path)
            assert "delayed_sip" not in text, str(path)

    def test_adapter_rejects_verified_non_iex_feed(self) -> None:
        client = _QuoteClient(_payload(feed="sip"))
        with pytest.raises(QuoteValidationError, match="non-IEX"):
            AlpacaPaperMarketData(client).get_latest_quote("SGOV")


class TestBuyOneSgovBinding:
    def test_ask_ten_bps_round_up_cents(self) -> None:
        quote = iex_quote(bid="100.69", ask="100.70")
        canonical = executable_canonical_price(quote, Side.BUY)
        bound = bind_buy(canonical, Decimal("1"))
        raw = Decimal("100.70") * (Decimal("1") + DEFAULT_BUY_LIMIT_TOLERANCE)
        expected = raw.quantize(CENT, rounding=ROUND_UP)
        assert expected == Decimal("100.81")
        assert bound.valuation_price == Decimal("100.70")
        assert bound.limit_price == expected
        assert bound.reference_price == bound.limit_price

    def test_maximum_cash_exposure_is_qty_times_limit(self) -> None:
        quote = iex_quote(bid="100.69", ask="100.70")
        canonical = executable_canonical_price(quote, Side.BUY)
        bound = bind_buy(canonical, Decimal("1"))
        assert bound.max_cash_obligation == bound.limit_price
        assert bound.max_cash_obligation == max_buy_cash_obligation(Decimal("1"), bound.limit_price)


class TestPreflightAndMutationScan:
    def test_preflight_contains_zero_mutation_capabilities(self) -> None:
        from opaca.broker.mutation import FORBIDDEN_BROKER_MUTATIONS

        source = (PRODUCTION_ROOT / "preflight.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        hits: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_BROKER_MUTATIONS:
                hits.append(node.attr)
            if isinstance(node, ast.FunctionDef) and node.name in FORBIDDEN_BROKER_MUTATIONS:
                hits.append(node.name)
        assert hits == []
        assert "execute_reserved_proposal" not in source
        assert "open_paper_execution_gateway_from_env" not in source

    def test_live_mutation_smoke_no_longer_calls_latest_trades(self) -> None:
        source = LIVE_MUTATION_TEST.read_text(encoding="utf-8")
        assert "latest_trades" not in source
        assert "get_latest_trade" not in source
        assert "get_stock_latest_trade" not in source
        assert "required_canonical_prices" in source
        assert "ASSET_UNIVERSE" not in source

    def test_fetch_freshness_is_the_hard_bound(self) -> None:
        from opaca.market import quote as quote_module

        assert DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS == 15
        assert not hasattr(quote_module, "DEFAULT_MAX_SOURCE_EVENT_AGE_SECONDS")

    def test_empty_account_required_symbols_are_sgov_only(self) -> None:
        assert required_pricing_symbols(("SGOV",), ()) == ("SGOV",)


class TestHeldValuationUsesBid:
    def test_held_only_symbol_uses_bid(self) -> None:
        quotes = {
            "SGOV": iex_quote("SGOV", bid="100.69", ask="100.70"),
            "BIL": iex_quote("BIL", bid="91.66", ask="91.67"),
        }
        canonical = canonical_prices_for_decision(quotes, side_by_symbol={"SGOV": Side.BUY})
        assert canonical["SGOV"].price == Decimal("100.70")
        assert canonical["BIL"].price == Decimal("91.66")
