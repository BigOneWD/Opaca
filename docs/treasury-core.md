# Opaca Treasury Core — Developer Notes (Phase 0/1)

Deterministic financial/control foundation only. No AI reasoning, no broker
mutation, no network. SPEC (`docs/SPEC.md`) wins over everything here.

## Pipeline

```text
reconciled broker cash (BrokerCashState)
  + dated obligations (Obligation, explicit ISO due_date)
  + derived settlement events (SettlementEvent, T+1)
        │
        ▼
compute_liquidity()            treasury/liquidity.py
        │  settled cash, protected liquidity, investable cash,
        │  available-by-date schedule, funding ceiling
        ▼
project_portfolio()            treasury/liquidity.py
        │  existing positions + proposed legs = projected post-trade state
        ▼
TreasuryGuardEngine.evaluate() policy/engine.py   (CHECK-00 … CHECK-16)
        ▼
decide_authority()             authority/engine.py
        AUTO | APPROVAL_REQUIRED | REJECT
```

## Core invariants

1. **Single ledger.** Alpaca `cash` is the only authoritative balance.
   Opaca never claims cash the broker has not reconciled.
2. **Buying power is not liquidity.** `BrokerCashState.buying_power` is
   carried for audit/display only. No treasury/policy code reads it
   (Phase −1A: buying_power = 4× cash = broker leverage). CHECK-06/CHECK-11.
3. **Settlement is derived.** Paper trading credits sale proceeds instantly
   (Phase −1B, B7); Opaca still treats them as unavailable until the derived
   T+1 settlement date on the US business-day calendar (weekends + exchange
   holidays). `settled_cash = broker_cash − unsettled_proceeds`.
4. **Seed once, never rescale.** Scenario ratios (24/14/40/22) are converted
   to absolute amounts once at initialization (`treasury/scenario.py`).
   Obligations and reserve stay absolute; later cash movements never rescale
   them.
5. **Long-only.** Projected quantities may never be negative; sells may never
   exceed reconciled `quantity_available`. Broker shorting capability is not
   an input (CHECK-16).
6. **Concentration denominator** is projected post-trade total invested
   market value (existing holdings + proposed fills), never proposal notional
   alone (CHECK-04).
7. **Fail closed.** Missing price, missing tradability state, unverified
   environment, or non-trading-day blackout arithmetic → violation.

## Money and rounding

* All financial values are `Decimal`; binary floats are rejected at the
  boundary (`domain/money.py`).
* `round_money` — cents, ROUND_HALF_UP (accounting values).
* `round_budget` — cents, ROUND_DOWN (budgets/notional; rounding never
  increases the intended budget, SPEC §8).
* `round_quantity` — 1e-9 shares, ROUND_DOWN (Phase −1B fractional precision).
* Scenario seeding rounds each absolute amount DOWN to cents; the investable
  surplus is the exact residual, so parts always sum to opening cash.

## Policy evaluation order

CHECK-00 (kill switch) short-circuits: NO NEW ORDER MAY BE SUBMITTED.
Otherwise checks run in order CHECK-01 … CHECK-16 and every hard failure is
reported. CHECK-07 is an **authority input** (soft): it cannot REJECT a
policy-valid proposal; it only decides AUTO vs APPROVAL_REQUIRED downstream.

CHECK-12 applies only to proposals with sell legs; coverage is evaluated per
obligation due date on the derived schedule (settled cash + proceeds settling
by the due date + proposed proceeds − obligations due).

CHECK-15 blackout uses exchange-local time (America/New_York) against the
calendar session close; it is deterministic, configurable, and may be
disabled (`PrecloseBlackoutConfig`).

## Authority

`decide_authority`: policy REJECT → REJECT; else the four dimensions
(per-order, per-proposal aggregate, rolling 24h notional, rolling 24h order
count) → AUTO if all pass, else APPROVAL_REQUIRED. Splitting cannot bypass:
aggregate covers intra-proposal splits, rolling windows cover cross-proposal
splits. `apply_human_approval` only ever promotes APPROVAL_REQUIRED; REJECT
can never be overridden (policy must re-run before submission, SPEC §10).
CHECK-13 (runaway hourly order count) is a hard REJECT in the policy engine.

## Partial-fill safety (`policy/partial_fill.py`)

* Buys: every non-empty subset of buy legs must remain CHECK-01/02/04/06/11
  compliant (single-leg fills concentrate fully in one symbol).
* Sells: coverage is evaluated for every fill subset including the empty
  (zero-fill) subset; `zero_fill_covers_obligations=False` means liquidity
  must never be represented as restored until actual fills reconcile and
  settle.

## Deterministic order identity (CHECK-09)

`client_order_id = "opaca-" + sha256(f"{proposal_id}:{leg_index}")[:32]` —
38 chars, alphanumeric/hyphen, within Alpaca limits verified in Phase −1B
(B3: hex IDs up to 128 chars accepted; duplicates rejected by broker).
Retrying a logical leg reproduces the same broker identifier.

## Calendar

`calendar/us_trading_calendar.py`: rule-based `USTradingCalendar` (weekday
rule + NYSE holiday/early-close tables 2025–2027) and `StaticTradingCalendar`
(authoritative session list, e.g. Alpaca calendar endpoint). Tests assert the
built-in calendar matches Phase −1 evidence exactly for 2026-08-28 …
2026-10-12 (31 sessions; only missing weekday 2026-09-07 / Labor Day).

## Running the gates (offline)

```sh
cd backend
.venv/bin/python -m pytest        # deterministic suite, no credentials
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy                    # strict
```
