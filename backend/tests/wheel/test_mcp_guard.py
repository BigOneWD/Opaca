"""RED-phase contracts for the exact read-only Alpaca MCP boundary."""

from __future__ import annotations

import pytest
from opaca.wheel.mcp_guard import (
    MCP_ALLOWED_TOOLS,
    MCP_REQUIRED_READ_TOOLS,
    McpToolSurfaceError,
    assert_mcp_tool_surface,
)

VERIFIED_TOOLS = frozenset(
    {
        "mcp__alpaca_readonly__fetch_alpaca_doc",
        "mcp__alpaca_readonly__get_all_assets",
        "mcp__alpaca_readonly__get_alpaca_endpoint_docs",
        "mcp__alpaca_readonly__get_asset",
        "mcp__alpaca_readonly__get_calendar",
        "mcp__alpaca_readonly__get_clock",
        "mcp__alpaca_readonly__get_corporate_action_announcement",
        "mcp__alpaca_readonly__get_corporate_action_announcements",
        "mcp__alpaca_readonly__get_crypto_bars",
        "mcp__alpaca_readonly__get_crypto_quotes",
        "mcp__alpaca_readonly__get_crypto_trades",
        "mcp__alpaca_readonly__get_market_movers",
        "mcp__alpaca_readonly__get_most_active_stocks",
        "mcp__alpaca_readonly__get_news",
        "mcp__alpaca_readonly__get_option_bars",
        "mcp__alpaca_readonly__get_option_chain",
        "mcp__alpaca_readonly__get_option_contract",
        "mcp__alpaca_readonly__get_option_contracts",
        "mcp__alpaca_readonly__get_option_exchange_codes",
        "mcp__alpaca_readonly__get_option_latest_quote",
        "mcp__alpaca_readonly__get_option_latest_trade",
        "mcp__alpaca_readonly__get_option_snapshot",
        "mcp__alpaca_readonly__get_option_trades",
        "mcp__alpaca_readonly__get_stock_bars",
        "mcp__alpaca_readonly__get_stock_latest_bar",
        "mcp__alpaca_readonly__get_stock_latest_quote",
        "mcp__alpaca_readonly__get_stock_latest_trade",
        "mcp__alpaca_readonly__get_stock_quotes",
        "mcp__alpaca_readonly__get_stock_snapshot",
        "mcp__alpaca_readonly__get_stock_trades",
        "mcp__alpaca_readonly__list_alpaca_api_endpoints",
        "mcp__alpaca_readonly__search_alpaca_api_specs",
        "mcp__alpaca_readonly__search_alpaca_docs",
    }
)


def test_verified_surface_is_exactly_the_33_tool_evidence_surface() -> None:
    assert len(VERIFIED_TOOLS) == 33
    assert MCP_ALLOWED_TOOLS == VERIFIED_TOOLS
    assert_mcp_tool_surface(VERIFIED_TOOLS, MCP_ALLOWED_TOOLS, MCP_REQUIRED_READ_TOOLS)


@pytest.mark.parametrize(
    "extra",
    [
        "mcp__alpaca_readonly__place_option_order",
        "mcp__alpaca_readonly__replace_order_by_id",
        "mcp__alpaca_readonly__unknown_mutation_style_tool",
    ],
)
def test_mutation_style_or_unrecognized_extra_tool_fails_closed(extra: str) -> None:
    with pytest.raises(McpToolSurfaceError):
        assert_mcp_tool_surface(
            VERIFIED_TOOLS | {extra},
            MCP_ALLOWED_TOOLS,
            MCP_REQUIRED_READ_TOOLS,
        )


def test_missing_required_read_tool_fails_closed() -> None:
    missing = next(iter(MCP_REQUIRED_READ_TOOLS))
    with pytest.raises(McpToolSurfaceError):
        assert_mcp_tool_surface(
            VERIFIED_TOOLS - {missing},
            MCP_ALLOWED_TOOLS,
            MCP_REQUIRED_READ_TOOLS,
        )
