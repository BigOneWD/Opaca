# Broker Reality Spike — Phase −1 Evidence Log

Spec reference: `docs/SPEC.md` §17. This file summarizes observations and names the
corresponding sanitized evidence files under `spike/evidence/`.

**Status: Phase −1A (read-only preflight) COMPLETE — 2026-08-28. No order has been submitted.**

Credentials are supplied via shell environment (`APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`,
paper keys). They are never printed, logged, or serialized; no `.env` files exist in the repo.


## Harness and safety gate (verified)

| Item | Observation |
| ---- | ----------- |
| Harness | `spike/spike.py` with explicit read-only subcommands `account`, `assets`, `clock`, `calendar` |
| Endpoint pin | Client constructed with `paper=True` only; gate asserts base URL prefix `https://paper-api.alpaca.markets`; no URL override path exists in the harness |
| Live endpoint | alpaca-py live constant is `https://api.alpaca.markets` — gate rejects it |
| Endpoint verified at runtime | Gate passed on all four A1–A4 runs (observed base URL = paper endpoint) |
| Fail-closed behavior | With credentials unset, every subcommand exits `BLOCKED` (exit code 2) before any broker call (observed 2026-08-28); an unconfirmable base URL also blocks (observed 2026-08-28 during harness fix) |
| Secret hygiene | Evidence files contain broker response fields only; account `id`/`account_number` redacted; no credentials anywhere in evidence |


## A1 — Account (observed 2026-08-28T13:36Z)

Evidence: `spike/evidence/account_20260828T133609Z.json`

| Field | Value |
| ----- | ----- |
| status | ACTIVE |
| currency | USD |
| cash | $100,000 |
| buying_power | $400,000 |
| regt_buying_power | $200,000 |
| non_marginable_buying_power | $100,000 |
| multiplier | 4 |
| equity / last_equity | $100,000 / $100,000 |
| portfolio_value | $100,000 |
| long_market_value / short_market_value | 0 / 0 |
| sma | $100,000 |
| trading_blocked / transfers_blocked / account_blocked | false / false / false |
| trade_suspended_by_user | false |
| shorting_enabled | **true** |
| pattern_day_trader / daytrade_count | null / null |
| crypto_status | ACTIVE |
| options_approved_level / options_trading_level | 3 / 3 |
| created_at | 2026-05-05 |

### Cash vs buying power comparison

* `cash` = $100,000 = `non_marginable_buying_power` (difference $0)
* `buying_power` − `cash` = **$300,000 of pure leverage** (multiplier 4)
* `regt_buying_power` = $200,000 (2× Reg-T) — also leveraged

### Demo-baseline probe (read-only)

* Observed starting balance: **$100,000**, not the preferred $500,000.
* Introspection of the alpaca-py `TradingClient` surface found **zero**
  reset/balance/deposit/transfer-related methods. The Trading API exposes no mechanism to
  configure or reset paper cash.
* No reset or balance-setting call was made.

**Approved decision (2026-08-28, human review):** the authoritative demo baseline is the
**actual Alpaca paper cash — $100,000**. Opaca does NOT require or depend on a manual
$500,000 broker reset. The demo scenario is seeded from opening broker cash using the frozen
ratios, converted to absolute amounts once at scenario initialization:

| Item | Ratio | Amount at $100,000 baseline |
| ---- | ----- | --------------------------- |
| payroll | 24% | $24,000 |
| suppliers | 14% | $14,000 |
| minimum operating reserve | 40% | $40,000 |
| initial investable surplus | 22% | $22,000 |

Manual $500,000 setup may be revisited only as optional final-demo polish.

### Architectural implications

1. **CHECK-06 is essential, empirically**: `buying_power` ($400k) is 4× actual corporate cash.
   Funding decisions from `buying_power` would silently authorize leverage. Opaca must fund
   from cash/`non_marginable_buying_power` semantics only.
2. **CHECK-11 is essential, empirically**: the paper account is margin-enabled
   (`multiplier: 4`, `shorting_enabled: true`). Opaca must constrain itself so no order can
   consume margin; account capability exceeds Opaca's permitted universe.
3. **CHECK-16 added (evidence-driven)**: because the account permits shorting
   (`shorting_enabled: true`), broker capability must never be read as policy permission.
   Opaca is long-only; see `docs/SPEC.md` §9 CHECK-16.
4. **Account capability exceeds policy universe**: crypto ACTIVE and options level 3 are
   enabled at the account level — §6 whitelist / CHECK-03 is the enforcement point.
5. **Amendment A resolved by decision**: the $100,000 actual cash is the scenario base;
   obligations/reserve seeded deterministically at the frozen ratios (see table above).


## A2 — Assets (observed 2026-08-28T13:36Z)

Evidence: `spike/evidence/assets_20260828T133619Z.json`

| Field | SGOV | BIL | SHV |
| ----- | ---- | --- | --- |
| status | active | active | active |
| tradable | true | true | true |
| fractionable | true | true | true |
| marginable | true | true | true |
| shortable | true | true | true |
| easy_to_borrow | true | true | true |
| exchange | NYSE | ARCA | NYSE |
| class | us_equity | us_equity | us_equity |
| maintenance_margin_requirement | 30.0 | 30.0 | 30.0 |
| attributes | fractional_eh_enabled, has_options, overnight_tradable | same | same |
| min_order_size / min_trade_increment / price_increment | null | null | null |

### Architectural implications

1. CHECK-05 satisfied for the full §6 universe.
2. Fractionable on all three — fractional and notional order support is plausible; must be
   verified empirically in Phase −1B.
3. Extended-hours-relevant attributes exist (`fractional_eh_enabled`, `overnight_tradable`);
   actual EH order behavior still needs Phase −1B verification.
4. Broker exposes **no minimum order size** — CHECK-14 (minimum trade size) must be enforced
   by Opaca policy, not relied on from the broker.
5. `has_options` attribute exists on the assets; §6 no-options rule is enforced by Opaca,
   not by the broker.


## A3 — Market Clock (observed 2026-08-28T13:36Z)

Evidence: `spike/evidence/clock_20260828T133628Z.json`

| Field | Broker raw (exchange-local, UTC offset) | SGT (display only) |
| ----- | --------------------------------------- | ------------------ |
| timestamp | 2026-08-28 09:36:27.913840-04:00 | 2026-08-28 21:36:27+08:00 |
| is_open | **true** | — |
| next_close | 2026-08-28 16:00:00-04:00 | 2026-08-29 04:00:00+08:00 |
| next_open | 2026-08-31 09:30:00-04:00 (Monday; weekend skipped) | 2026-08-31 21:30:00+08:00 |

### Architectural implications

1. Broker timestamps are exchange-local (America/New_York, EDT −04:00 at observation time),
   not UTC. Opaca must normalize to UTC internally and render SGT for display only.
2. Clock is suitable for CHECK-15 pre-close blackout gating and session awareness.
3. Weekend handling confirmed at clock level (next_open rolls Friday → Monday).


## A4 — Trading Calendar (observed 2026-08-28T13:37Z)

Evidence: `spike/evidence/calendar_20260828T133740Z.json`

* Requested window: 2026-08-28 → 2026-10-12 (45 days)
* Sessions returned: 31
* Weekend dates in returned sessions: none (weekends excluded by the API)
* Missing weekdays within window: **2026-09-07 only** (US Labor Day)
* Early closes in window: none observed (all sessions close 16:00 local)

### Architectural implications

1. The calendar endpoint is suitable as the authoritative business-day source for Opaca's
   derived T+1 settlement schedule (§5, Amendment B): weekend rolls and holiday exclusions
   are directly observable.
2. Early-close detection must compare session close times (this window has none; e.g. the
   day-after-Thanksgiving session would show 13:00).
3. No production settlement logic was implemented in the spike, per scope.


## Unresolved items (carried into Phase −1B / demo setup)

1. **Demo balance**: RESOLVED by approved decision — actual $100,000 cash is the
   authoritative baseline; scenario seeded at frozen ratios; no manual reset dependency.
2. Order behavior (market/limit/fractional/notional), `client_order_id` constraints and
   duplicate behavior, lifecycle statuses, UNKNOWN recovery, and sell/settlement crediting
   are all untested — Phase −1B, each behind an explicit subcommand and human approval.
3. Settlement crediting behavior in paper (instant vs delayed) — Phase −1B sell experiment;
   Opaca's derived schedule remains authoritative regardless (§5).


## Evidence files

```text
spike/evidence/account_20260828T133609Z.json
spike/evidence/assets_20260828T133619Z.json
spike/evidence/clock_20260828T133628Z.json
spike/evidence/calendar_20260828T133740Z.json
```
