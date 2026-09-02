# Wheel Read-Only Feasibility

Probe date: 2026-09-02

This artifact records read-only observations only. No order submission,
cancellation, replacement, exercise, account mutation, or destructive Alpaca
tool was invoked.

## Repository

- branch: `feat/wheel-competition-mode`
- HEAD before this evidence artifact: `6a423bcd8ccddc42f35a7d6a4a01335f6ec8c248`
- clean/dirty before this evidence artifact: clean
- the evidence file below is the only intended working-tree change before the evidence commit

## Alpaca Environment

- PAPER verified: yes
- `APCA_API_BASE_URL` exactly matched `https://paper-api.alpaca.markets`: yes
- `TradingClient(..., paper=True)` base endpoint matched the PAPER endpoint: yes
- credentials present: yes (`APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` presence checked only)
- no secret values: printed or persisted: no
- installed `alpaca-py`: `0.33.0`

## Account

- cash: `99899.58` USD
- `options_buying_power` presence/value: present; `99949.78` USD
- `options_approved_level`: `3`
- `options_trading_level`: `3`
- trading status: `ACTIVE`
- `trading_blocked`: `false`
- `trade_suspended_by_user`: present; `false`
- sanitized account fingerprint: `a1facbe1522d` (first 12 hex characters of SHA-256 of the full broker account id)
- full account id/account number: not recorded

## Options Contract Reality

- plausible liquid underlying tested: `SPY`
- underlying latest trade price: `764.525` USD
- underlying latest trade timestamp: `2026-09-02 14:17:53.435922+00:00` (IEX feed)
- bounded contract query: active PUTs for `2026-09-03` through `2026-09-09`, strike `714.0` through `764.525`, limit `30`
- active PUT contracts returned by the bounded query: `30` (response limit)
- observed contract OCC symbol: `SPY260903P00746000`
- PUT: yes (`type=put`)
- underlying: `SPY`
- strike: `746.0` USD
- expiration: `2026-09-03`
- multiplier: `100`, explicitly present in the raw Trading API contract payload
- status/tradability: `active` / `true`
- multiplier adapter warning: the typed `alpaca.trading.models.OptionContract` object in `alpaca-py==0.33.0` does not expose a `multiplier` attribute, although the raw API response does. Downstream adaptation must retain the raw multiplier or fail closed; it must not assume `100`.

## Option Quote Reality

- quote API: `OptionHistoricalDataClient.get_option_latest_quote`
- quote feed: `indicative`
- observed contract: `SPY260903P00746000`
- bid: `0.07` USD
- ask: `0.08` USD
- quote timestamp: `2026-09-02 14:17:55.762544+00:00`
- timestamp field name: `timestamp`
- timezone-aware: yes (`UTC` offset present)
- OPRA probe: read-only request returned `APIError`; OPRA agreement/entitlement was unavailable. The indicative quote above was returned successfully. This entitlement must be resolved or explicitly handled before any execution-quality quote gate is relied on.

## Market Clock

- open/closed: open (`is_open=true`)
- current market timestamp: `2026-09-02 10:17:53.799766-04:00`
- next open: `2026-09-03 09:30:00-04:00`
- next close: `2026-09-02 16:00:00-04:00`

## MCP

- installed/configured: no
- local Codex MCP configuration inspected: `/Users/macmini/.codex/config.toml`
- configured local MCP server sections: `computer-use`, `node_repl`
- Alpaca MCP configuration entry present: no
- `alpaca_mcp_server` package present in the project virtual environment: no
- `uvx` executable present: yes, but the Alpaca MCP server was not invoked or installed
- enabled tool names containing `alpaca`: none
- exact exposed Alpaca tool names: none available to enumerate because no Alpaca server is configured
- exact locally exposed read-only tools: none
- mutation-capable tools exposed locally: no; no Alpaca server is exposed
- no mutation tools were invoked

The official Alpaca MCP V2 documentation identifies the local server as
`uvx alpaca-mcp-server`. For a future strict observation lane, the smallest
documented toolset configuration for this project is:

```toml
[mcp_servers.alpaca-readonly]
command = "uvx"
args = ["alpaca-mcp-server"]

[mcp_servers.alpaca-readonly.env]
ALPACA_API_KEY = "<secure mapping of APCA_API_KEY_ID>"
ALPACA_SECRET_KEY = "<secure mapping of APCA_API_SECRET_KEY>"
ALPACA_PAPER_TRADE = "true"
ALPACA_TOOLSETS = "assets,stock-data,options-data,news"
```

After activation and client restart, the exact server-emitted names must be
captured and committed as `MCP_ALLOWED_TOOLS`. Based on the official V2 tool
registry, the read-only candidate names for those toolsets are:

- assets: `get_all_assets`, `get_asset`, `get_option_contracts`, `get_option_contract`, `get_calendar`, `get_clock`, `get_corporate_action_announcements`, `get_corporate_action_announcement`
- stock-data: `get_stock_bars`, `get_stock_quotes`, `get_stock_trades`, `get_stock_latest_bar`, `get_stock_latest_quote`, `get_stock_latest_trade`, `get_stock_snapshot`, `get_most_active_stocks`, `get_market_movers`
- options-data: `get_option_bars`, `get_option_trades`, `get_option_latest_trade`, `get_option_latest_quote`, `get_option_snapshot`, `get_option_chain`, `get_option_exchange_codes`
- news: `get_news`

These are documented candidates, not locally exposed names. The official
server's unfiltered surface also includes mutation-capable tools such as
`place_option_order`, `cancel_order_by_id`, `replace_order_by_id`,
`exercise_options_position`, and account/position mutation tools; they were
not enabled or invoked in this probe.

Strict read-only allowlist technically enforceable: yes in the documented V2
design through `ALPACA_TOOLSETS` plus an exact application-side allowlist, but
not locally verified until the server is configured and its actual tool list
is enumerated.

Official setup references:

- [Alpaca official MCP Server V2 README](https://github.com/alpacahq/alpaca-mcp-server#configuration)
- [Alpaca official agentic repository](https://github.com/alpacahq/agentic#run-trading-mcp-locally)

## Feasibility Decision

BLOCKED BEFORE TASK 2

Concrete blockers:

1. Alpaca MCP is not installed/configured in the local Codex agent, so the exact exposed read-only tool surface cannot yet be enumerated or frozen into `MCP_ALLOWED_TOOLS`.
2. OPRA option quotes are unavailable for this account because the OPRA agreement/entitlement is not signed. A read-only indicative quote is available, but execution-quality quote semantics remain to be resolved before later policy/execution work.

Smallest next action: explicitly configure the official local V2 server with
`ALPACA_PAPER_TRADE=true` and the minimal read-only toolsets
`assets,stock-data,options-data,news`, restart/reload the MCP client, enumerate
the actual emitted names, and verify no mutation tool is exposed. Separately
confirm the permitted option quote feed/entitlement before any later execution
task. Do not start Task 2 until those observations are captured.
