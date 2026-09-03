"""Exact application-side allowlist for the verified Alpaca MCP surface."""

from __future__ import annotations

from collections.abc import Iterable


class McpToolSurfaceError(ValueError):
    """The configured MCP surface is incomplete or broader than allowed."""


MCP_ALLOWED_TOOLS = frozenset(
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

MCP_REQUIRED_READ_TOOLS = frozenset(
    {
        "mcp__alpaca_readonly__get_clock",
        "mcp__alpaca_readonly__get_option_contracts",
        "mcp__alpaca_readonly__get_option_latest_quote",
    }
)


def _tool_set(values: Iterable[str], label: str) -> frozenset[str]:
    result = frozenset(values)
    if any(not isinstance(value, str) or not value.strip() for value in result):
        raise McpToolSurfaceError(f"{label} contains an invalid tool name")
    return result


def assert_mcp_tool_surface(
    exposed: Iterable[str],
    allowed: Iterable[str],
    required: Iterable[str],
) -> None:
    """Enforce both completeness and least privilege before provider use."""
    exposed_tools = _tool_set(exposed, "exposed")
    allowed_tools = _tool_set(allowed, "allowed")
    required_tools = _tool_set(required, "required")
    missing = required_tools - exposed_tools
    unexpected = exposed_tools - allowed_tools
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append(f"missing required tools: {sorted(missing)}")
        if unexpected:
            details.append(f"unrecognized exposed tools: {sorted(unexpected)}")
        raise McpToolSurfaceError("; ".join(details))


__all__ = [
    "MCP_ALLOWED_TOOLS",
    "MCP_REQUIRED_READ_TOOLS",
    "McpToolSurfaceError",
    "assert_mcp_tool_surface",
]
