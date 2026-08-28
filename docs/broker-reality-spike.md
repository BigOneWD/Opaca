# Broker Reality Spike — Phase −1 Evidence Log

Spec reference: `docs/SPEC.md` §17. This file summarizes observations and names the
corresponding sanitized evidence files under `spike/evidence/`.

**Status: Phase −1A (read-only preflight) and Phase −1B (mutating experiments) COMPLETE —
2026-08-28. All experiments paper-only, explicitly invoked, minimum practical sizes.**

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
2. Order behavior, `client_order_id` behavior, lifecycle statuses, UNKNOWN recovery, and
   sell/settlement crediting — **completed in Phase −1B below**.
3. Extended-hours live behavior — UNVERIFIED / NON-BLOCKING (see B8).


## Phase −1B — Mutating experiments (2026-08-28, ~13:56–13:59 UTC, market OPEN)

All experiments: explicitly invoked subcommands, minimum practical sizes, deterministic
`client_order_id = sha256(opaca-spike:<experiment>:<run-id>:<leg>)[:32]`, pre/post state
captured, single submission per leg (never auto-retried), long-only sells.

Pre-state (snapshot before first mutation): `spike/evidence/snapshot_20260828T135604Z.json`
— cash $100,000, no positions, no open orders.

### B1 — Whole-share market buy

Evidence: `spike/evidence/b1_market_buy_20260828T135616Z.json`

* Request: BUY 1 SGOV, market, DAY, deterministic client_order_id.
* Lifecycle observed: `new → filled` (no `accepted`/`pending_new` observed between polls).
* Fill: qty 1 @ $100.70. Cash $100,000 → $99,899.30. Position SGOV 1 long reconciled.
* Conclusion: whole-share market orders fill instantly during RTH; reconciliation gate passed.

### B2 — Whole-share limit + cancel

Evidence: `spike/evidence/b2_limit_cancel_20260828T135728Z.json`

* Request: BUY 1 SGOV limit $50.35 (deliberately non-marketable, ~50% of last trade), DAY.
* Open phase: status `new` (stable across polls). Cancel request → final status `canceled`.
* Cash and positions unchanged after cancellation. Gate passed.
* Conclusion: cancel lifecycle is clean; resting status observed is `new`.

### B3 — Duplicate client_order_id

Evidence: `spike/evidence/b3_duplicate_id_20260828T135751Z.json`

* First submission accepted (non-marketable limit, deterministic ID).
* Second submission with the **identical** client_order_id: REJECTED —
  `APIError {"code":42210000,"message":"client_order_id must be unique"}`. No second order created.
* Exactly one open order carried the ID; lookup by client_order_id returned the single order.
* ID constraint probes (non-marketable limits, canceled immediately after acceptance):
  client_order_id lengths **48, 49, 64, 128 all ACCEPTED**. No 48-char ceiling observed;
  charset hex accepted. Our 32-char hex encoding satisfies the constraint with margin.
* First order canceled at end; account unchanged.
* Conclusion: broker enforces client_order_id uniqueness at submission — deterministic IDs
  give broker-level duplicate prevention (CHECK-09 validated).

### B4 — Fractional quantity

Evidence: `spike/evidence/b4_fractional_20260828T135811Z.json`

* Request: BUY 0.1 SGOV market DAY. Accepted; lifecycle `filled`; fill 0.1 @ $100.698.
* Position became 1.1 (whole + fractional combined in one position).
* Conclusion: fractional qty works with market/DAY; no separate order type needed.

### B5 — Notional order

Evidence: `spike/evidence/b5_notional_20260828T135824Z.json`

* Probe $0.50: REJECTED — `notional amount must be >= 1.00` → **minimum notional is $1.00**.
* Main $10: accepted; lifecycle `filled`; resulting qty 0.099207531 @ $100.698.
* Position became 1.199207531. Market DAY is the accepted form.
* Conclusion: notional orders work; enforce ≥ $1.00 and Opaca's own dust floor (CHECK-14).

### B6 — UNKNOWN / crash recovery

Evidence: `spike/evidence/b6_unknown_recovery_20260828T135843Z.json`

* Non-marketable limit submitted once with deterministic ID; local order confirmation
  discarded by design (simulated loss).
* Recovery: bounded retry/backoff query by client_order_id → **found on attempt 1**.
* Zero second submissions (by design and verified: single order for the ID).
* Order canceled after evidence capture; account unchanged.
* Conclusion: recovery by client_order_id works; UNKNOWN_REQUIRES_REVIEW path implemented
  for the not-found branch (no auto-resubmission either way).

### B7 — Settlement sell / cleanup

Evidence: `spike/evidence/b7_settlement_sell_20260828T135900Z.json`

* Pre-state: SGOV 1.199207531 long (reconciled), cash $99,879.24.
* Sold exactly the reconciled long quantity (CHECK-16 compliant: long-only, no short created).
* Lifecycle `new → filled` @ $100.69. Position flat.
* Cash behavior: credited to $99,999.99 **immediately at terminal status**; unchanged +5s;
  no separate settled/unsettled/transferable fields exposed by the paper account API.
* Conclusion: **Alpaca paper trading credits sale proceeds instantly** — unrealistically vs
  real T+1 settlement. Per §5 (Amendment B), Opaca derives T+1 availability from its own
  business-day calendar over reconciled fills and does NOT treat broker crediting as
  legal/operational settlement. CHECK-12 must evaluate the derived schedule.

### B8 — Extended hours

Evidence: `spike/evidence/b8_extended_hours_20260828T135928Z.json`

* Asset attributes confirmed: `fractional_eh_enabled`, `overnight_tradable` (all three ETFs);
  order request models expose an `extended_hours` field; EH semantics require limit orders.
* No live outside-RTH order placed (market open during spike; not worth hackathon time).
* Status: **UNVERIFIED / NON-BLOCKING**. Opaca default: submit during RTH only.

### Phase −1B status ledger (observed Alpaca order statuses)

Observed across B1–B7: `new`, `filled`, `canceled`. Not observed in this spike:
`accepted`, `pending_new`, `partially_filled`, `rejected` (order-level), `expired`,
`done_for_day`, `held`. §13 mapping table must map the full documented set; unmapped
statuses fail closed to UNKNOWN.

### Account mechanics learned during B1–B7

* While holding marginable SGOV: `initial_margin`/`maintenance_margin` become non-zero and
  `non_marginable_buying_power = cash + loan value of positions` (exceeded cash by $50.34).
* Implication: **no broker buying-power field equals corporate cash once positions exist.**
  Opaca funding must be computed from reconciled broker cash itself (CHECK-06), never from
  any `*buying_power` field.
* Paper run-of-show net effect: cash $100,000 → $99,999.99 (1¢ spread loss across buys/sells).

## Evidence files

Phase −1A:

```text
spike/evidence/account_20260828T133609Z.json
spike/evidence/assets_20260828T133619Z.json
spike/evidence/clock_20260828T133628Z.json
spike/evidence/calendar_20260828T133740Z.json
```

Phase −1B:

```text
spike/evidence/snapshot_20260828T135604Z.json   (pre-mutation state)
spike/evidence/b1_market_buy_20260828T135616Z.json
spike/evidence/b2_limit_cancel_20260828T135728Z.json
spike/evidence/b3_duplicate_id_20260828T135751Z.json
spike/evidence/b4_fractional_20260828T135811Z.json
spike/evidence/b5_notional_20260828T135824Z.json
spike/evidence/b6_unknown_recovery_20260828T135843Z.json
spike/evidence/b7_settlement_sell_20260828T135900Z.json
spike/evidence/b8_extended_hours_20260828T135928Z.json
spike/evidence/snapshot_20260828T135930Z.json   (final state)
```
