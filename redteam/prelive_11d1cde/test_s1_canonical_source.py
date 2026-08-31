"""S1: the live PAPER path prices from Alpaca IEX latest trade, read-only."""
from __future__ import annotations

import ast
import os
import pathlib
import types

import pytest
from opaca.broker.errors import PaperEnvironmentError
from opaca.market.errors import MarketDataUnavailableError, QuoteValidationError
from opaca.market.quote import QUOTE_SOURCE_LATEST_TRADE
from opaca.market.source import AlpacaPaperMarketData, FakeMarketData, latest_trades

PKG = pathlib.Path(os.environ["OPACA_BACKEND"]) / "opaca"
PROD = sorted(PKG.rglob("*.py"))


def test_production_never_imports_test_fixtures_or_default_prices():
    offenders = []
    for m in PROD:
        text = m.read_text(encoding="utf-8")
        if "DEFAULT_PRICES" in text or "from tests" in text or "import tests" in text:
            offenders.append(str(m.relative_to(PKG)))
    assert offenders == []


def test_market_layer_has_no_synthetic_price_constant():
    for m in sorted((PKG / "market").rglob("*.py")):
        tree = ast.parse(m.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "Decimal" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    assert arg.value in {"0", "1", "0.001"}, (m.name, arg.value)


def test_the_only_market_data_client_is_the_read_only_historical_client():
    """AST-level: no TradingClient / mutator name is referenced in code."""
    tree = ast.parse((PKG / "market" / "source.py").read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value.count(" ") == 0:
            names.add(node.value)
    assert "StockHistoricalDataClient" in names
    assert "TradingClient" not in names
    assert not {"submit_order", "cancel_order_by_id", "cancel_order",
                "close_position", "close_all_positions"} & names


def test_latest_trade_requests_the_iex_feed():
    calls = []

    class Reader:
        def get_stock_latest_trade(self, request):
            calls.append(request)
            return {"SGOV": {"price": "100.69", "timestamp": "2026-09-01T14:29:59Z"}}

    src = AlpacaPaperMarketData(Reader())
    got = src.get_latest_trade("SGOV")
    assert got.source == QUOTE_SOURCE_LATEST_TRADE
    assert str(got.price) == "100.69"
    assert len(calls) == 1
    feed = getattr(calls[0], "feed", None)
    assert feed is not None and str(getattr(feed, "value", feed)).lower() == "iex"
    assert getattr(calls[0], "symbol_or_symbols", None) == "SGOV"


def test_the_read_only_source_retains_no_client_attribute():
    class Reader:
        def get_stock_latest_trade(self, request):
            raise AssertionError("not called")

    src = AlpacaPaperMarketData(Reader())
    assert AlpacaPaperMarketData.__slots__ == ("_get_stock_latest_trade",)
    assert not hasattr(src, "_client")
    assert not hasattr(src, "__dict__")
    for forbidden in ("submit_order", "cancel_order_by_id", "close_position",
                      "close_all_positions", "replace_order"):
        assert not callable(getattr(src, forbidden, None))


def test_a_missing_trade_raises_and_never_substitutes_a_price():
    class Empty:
        def get_stock_latest_trade(self, request):
            return {}

    with pytest.raises(MarketDataUnavailableError):
        AlpacaPaperMarketData(Empty()).get_latest_trade("SGOV")


def test_an_unknown_symbol_is_refused_before_any_network_call():
    class Boom:
        def get_stock_latest_trade(self, request):
            raise AssertionError("must not reach the network")

    with pytest.raises(QuoteValidationError):
        AlpacaPaperMarketData(Boom()).get_latest_trade("TSLA")
    with pytest.raises(QuoteValidationError):
        FakeMarketData().get_latest_trade("TSLA")


def test_unavailable_market_data_has_no_fallback():
    src = FakeMarketData(unavailable=True)
    with pytest.raises(MarketDataUnavailableError):
        src.get_latest_trade("SGOV")
    with pytest.raises(MarketDataUnavailableError):
        latest_trades(src, ("SGOV", "BIL", "SHV"))


def test_open_from_env_requires_credentials(monkeypatch):
    from opaca.broker.paper import ENV_KEY_ID, ENV_SECRET
    from opaca.market.source import open_paper_market_data_from_env

    monkeypatch.delenv(ENV_KEY_ID, raising=False)
    monkeypatch.delenv(ENV_SECRET, raising=False)
    with pytest.raises(PaperEnvironmentError):
        open_paper_market_data_from_env()


def test_no_production_module_imports_a_generic_http_client():
    banned = {"requests", "httpx", "urllib", "urllib3", "http", "socket", "aiohttp"}
    hits = []
    for m in PROD:
        tree = ast.parse(m.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in banned:
                        hits.append((m.name, a.name))
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in banned:
                    hits.append((m.name, node.module))
    assert hits == []


def test_module_import_does_not_touch_the_network():
    import opaca.market.source as mod
    assert isinstance(mod, types.ModuleType)
    src = (PKG / "market" / "source.py").read_text(encoding="utf-8")
    top = ast.parse(src)
    for node in top.body:
        assert not isinstance(node, (ast.Import, ast.ImportFrom)) or \
            not (node.module or "").startswith("alpaca") if isinstance(node, ast.ImportFrom) \
            else True
