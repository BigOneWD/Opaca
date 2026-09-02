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

- installed/configured: yes; active in this fresh Codex session
- official server: Alpaca MCP Server V2 `2.3.1` (resolved through `uvx`)
- local Codex MCP configuration inspected: `/Users/macmini/.codex/config.toml`
- configuration method: `codex mcp add alpaca-readonly -- /Users/macmini/.local/bin/opaca-alpaca-mcp-readonly`
- `codex mcp get alpaca-readonly`: enabled; stdio; launcher command above; no Codex-config credential environment values
- configured local MCP server sections in the file: `alpaca-readonly`, `computer-use`, `node_repl`
- other `codex mcp list` entries observed: `cua_repl` (app-managed), plus the three file entries above; existing entries were not changed
- Alpaca MCP configuration entry present: yes
- launcher path: `/Users/macmini/.local/bin/opaca-alpaca-mcp-readonly`
- launcher permissions: `700`
- launcher source credential mapping: `APCA_API_KEY_ID` -> `ALPACA_API_KEY`; `APCA_API_SECRET_KEY` -> `ALPACA_SECRET_KEY`; values are loaded only at runtime
- `ALPACA_PAPER_TRADE`: `true` (hard-set by launcher; value verified by source inspection)
- `ALPACA_TOOLSETS`: `assets,stock-data,options-data,news` (hard-set by launcher; exact value verified by source inspection)
- `alpaca_mcp_server` package present in the project virtual environment: no
- `uvx` executable present: yes; official Alpaca MCP Server V2 `2.3.1` was resolved in its isolated UV environment
- enabled tool names containing `alpaca` in this current Codex session: `33`
- exact exposed Alpaca tool names and classification:
  - `mcp__alpaca_readonly__fetch_alpaca_doc` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_all_assets` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_alpaca_endpoint_docs` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_asset` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_calendar` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_clock` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_corporate_action_announcement` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_corporate_action_announcements` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_crypto_bars` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_crypto_quotes` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_crypto_trades` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_market_movers` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_most_active_stocks` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_news` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_option_bars` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_option_chain` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_option_contract` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_option_contracts` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_option_exchange_codes` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_option_latest_quote` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_option_latest_trade` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_option_snapshot` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_option_trades` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_stock_bars` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_stock_latest_bar` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_stock_latest_quote` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_stock_latest_trade` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_stock_quotes` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_stock_snapshot` — `READ_ONLY`
  - `mcp__alpaca_readonly__get_stock_trades` — `READ_ONLY`
  - `mcp__alpaca_readonly__list_alpaca_api_endpoints` — `READ_ONLY`
  - `mcp__alpaca_readonly__search_alpaca_api_specs` — `READ_ONLY`
  - `mcp__alpaca_readonly__search_alpaca_docs` — `READ_ONLY`
- toolset scope: the broker/data tools are limited to `assets`, `stock-data`, `options-data`, and `news`; the five documentation/spec helpers are read-only reference utilities and do not execute broker endpoints
- mutation-capable or ambiguous tools exposed: `NO`
- exposed equivalents of place/replace/cancel/close/exercise/account-configuration mutation: `NONE`
- read-only smoke tool used: `mcp__alpaca_readonly__get_clock`
- read-only smoke result: `SUCCESS`; returned `is_open=true`, `next_open`, `next_close`, and a current timestamp; MCP result had no error
- no mutation tools were invoked

The Codex CLI's actual supported configuration is a global stdio server
registration. The persisted entry is equivalent to:

```toml
[mcp_servers.alpaca-readonly]
command = "/Users/macmini/.local/bin/opaca-alpaca-mcp-readonly"
```

The external launcher contains the runtime-only mapping and exact policy:

```text
ALPACA_PAPER_TRADE=true
ALPACA_TOOLSETS=assets,stock-data,options-data,news
exec /Users/macmini/.local/bin/uvx alpaca-mcp-server
```

The documented V2 candidate names for the four configured toolsets are:

- assets: `get_all_assets`, `get_asset`, `get_option_contracts`, `get_option_contract`, `get_calendar`, `get_clock`, `get_corporate_action_announcements`, `get_corporate_action_announcement`
- stock-data: `get_stock_bars`, `get_stock_quotes`, `get_stock_trades`, `get_stock_latest_bar`, `get_stock_latest_quote`, `get_stock_latest_trade`, `get_stock_snapshot`, `get_most_active_stocks`, `get_market_movers`
- options-data: `get_option_bars`, `get_option_trades`, `get_option_latest_trade`, `get_option_latest_quote`, `get_option_snapshot`, `get_option_chain`, `get_option_exchange_codes`
- news: `get_news`

The current fresh-session inventory above is authoritative for this
verification. It additionally exposes three read-only crypto-data tools and
five read-only documentation/spec helpers. The official server's unfiltered
surface also includes mutation-capable tools such as
`place_option_order`, `cancel_order_by_id`, `replace_order_by_id`,
`exercise_options_position`, and account/position mutation tools; they were
not enabled or invoked in this probe.

Strict read-only allowlist technically enforceable: yes in the documented V2
design through `ALPACA_TOOLSETS` plus an exact application-side allowlist, but
proven in this fresh Codex session by the exact 33-tool inventory above and the
successful market-clock smoke test.

Official setup references:

- [Alpaca official MCP Server V2 README](https://github.com/alpacahq/alpaca-mcp-server#configuration)
- [Alpaca official agentic repository](https://github.com/alpacahq/agentic#run-trading-mcp-locally)

## Feasibility Decision

READY FOR TASK 2
WITH PRE-MUTATION OPRA BLOCKER

All MCP checks passed: the configured server is active in the fresh session,
the exact exposed surface is read-only, and the single market-clock smoke test
succeeded. OPRA remains unavailable; option pricing is available only through
the indicative feed. OPRA does not block local TDD implementation, but it does
block the eventual real CSP proof until OPRA is resolved or the pricing
requirement is explicitly reconsidered.
