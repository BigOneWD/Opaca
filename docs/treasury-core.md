# Opaca Treasury Core — Developer Notes (Phase 0/1)

Deterministic financial/control foundation only. No AI reasoning, no broker
mutation, no network. SPEC (`docs/SPEC.md`) wins over everything here.

Red-team remediation applied (RT-01 … RT-10, verdict PASS WITH FINDINGS /
FIX THEN RETEST): `docs/red-team/treasury-core-attack-plan.md` lives on the
`review/treasury-red-team` branch.

## Pipeline

```text
reconciled broker cash (BrokerCashState)
  + dated obligations (Obligation, explicit ISO due_date)
  + derived settlement events (SettlementEvent, T+1)
        │
        ▼
compute_liquidity()            treasury/liquidity.py
        │  settled cash, protected liquidity, investable cash,
        │  available-by-date schedule, funding ceiling,
        │  investment_pool_base denominator
        ▼
project_portfolio()            treasury/liquidity.py
        │  existing positions + proposed legs = projected post-trade state
        ▼
TreasuryGuardEngine.evaluate() policy/engine.py   (CHECK-00 … CHECK-16)
        ▼
decide()                       policy/decision.py
        │  evaluate -> partial-fill safety -> authority   (RT-06)
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
5. **Long-only, reservation-aware (RT-01).** Projected quantities may never
   be negative; sells may never exceed the reservation-aware available
   quantity:

   ```text
   effective_available(symbol) =
       min(
           broker quantity_available,
           reconciled position quantity
             - locally reserved unresolved SELL remaining quantity
       )
   ```

   The `min` avoids double subtraction when Alpaca has already decremented
   `quantity_available` for an acknowledged order. Unresolved SELL states
   (pending/new, accepted/live, partially filled, UNKNOWN, pre-submission
   states) reserve their REMAINING quantity; an undeterminable remaining
   quantity fails closed for further sells of that symbol. Broker shorting
   capability is not an input (CHECK-16).

   *Orchestration invariant:* two truly simultaneous evaluations against the
   same snapshot still require an ATOMIC SQLite reservation before broker
   submission. No broker execution may be added until the execution layer
   performs `evaluate -> reserve -> persist` under a single-writer
   transaction. The stateless engine alone does not solve simultaneous
   callers.
6. **Concentration denominator is the INVESTMENT POOL BASE (Amendment G,
   RT-02)** — never proposal notional alone, never total corporate cash:

   ```text
   investment_pool_base =
       current market value of eligible investment holdings
       + current deployable investment cash
   ```

   Deployable investment cash is the settlement-aware investable cash
   (settled cash minus protected reserve and obligation-committed cash);
   negative investable cash contributes zero. The base is fixed at proposal
   evaluation time and stays the denominator for every partial-fill subset,
   so unfilled investment cash prevents fake 100% concentrations. Sells
   reduce concentration; a full liquidation passes without a special
   vacuous branch (CHECK-04).
7. **Fail closed.** Missing price, missing tradability state, unverified
   environment, undeterminable sell reservation, or a missing/out-of-range
   trading session → violation.

## Money and rounding

* All financial values are `Decimal`; binary floats are rejected at the
  boundary (`domain/money.py`).
* `round_money` — cents, ROUND_HALF_UP (accounting values).
* `round_budget` — cents, ROUND_DOWN (budgets/notional; rounding never
  increases the intended budget, SPEC §8).
* `round_quantity` — 1e-9 shares, ROUND_DOWN (Phase −1B fractional precision).
* Magnitude boundary (RT-05): values at/above `MAGNITUDE_LIMIT` (1e26) are
  rejected with `MoneyError` at the validation boundary, and every quantize
  boundary converts `decimal.InvalidOperation` to `MoneyError`. A raw
  `InvalidOperation` never escapes a public money function.
* Scenario seeding rounds each absolute amount DOWN to cents; the investable
  surplus is the exact residual, so parts always sum to opening cash.

## Policy evaluation order

CHECK-00 (kill switch) short-circuits: NO NEW ORDER MAY BE SUBMITTED.
Otherwise checks run in order CHECK-01 … CHECK-16 and every hard failure is
reported. CHECK-07 is an **authority input** (soft): it cannot REJECT a
policy-valid proposal; it only decides AUTO vs APPROVAL_REQUIRED downstream.

CHECK-12 applies only to proposals with sell legs; coverage is evaluated per
obligation due date on the derived schedule (settled cash + proceeds settling
by the due date + proposed proceeds − obligations due). When the calendar
cannot derive a settlement date (e.g. outside its supported range), CHECK-02
and CHECK-12 fail closed.

CHECK-15 (RT-07) is a market session gate first and a blackout second:
trading-day validity is unconditional — Saturday/Sunday/exchange holiday →
fail closed even when `PrecloseBlackoutConfig.enabled = False`; when enabled,
the pre-close window is additionally enforced against the session close in
exchange-local time (America/New_York).

CHECK-16 (RT-01) aggregates proposed sell legs per symbol and bounds them by
the reservation-aware available quantity (see invariant 5).

### Partial evaluation surface (RT-10)

`evaluate(only=...)` is reserved for the internal subset evaluator. Any
evaluation that intentionally skipped hard checks is marked
`PolicyDecision.complete = False` and can never report `passed = True`;
callers must read `results`/`violations`, never `passed`, from a partial
evaluation.

## Authority

`decide_authority`: policy REJECT → REJECT; else partial-fill safety gate
(RT-06): no assessment → REJECT (fail closed), UNSAFE assessment → REJECT
(hard safety failure, never AUTO, not overridable by human approval); else
the four dimensions (per-order, per-proposal aggregate, rolling 24h notional,
rolling 24h order count) → AUTO if all pass, else APPROVAL_REQUIRED.
Splitting cannot bypass: aggregate covers intra-proposal splits, rolling
windows cover cross-proposal splits. `apply_human_approval` only ever
promotes APPROVAL_REQUIRED; REJECT can never be overridden (policy must
re-run before submission, SPEC §10). CHECK-13 (runaway hourly order count)
is a hard REJECT in the policy engine.

`policy/decision.py::decide()` is the single authority entry point:
`evaluate -> assess_partial_fill_safety -> decide_authority`.

## Partial-fill safety (`policy/partial_fill.py`)

* Every non-empty subset of ALL legs (buys and sells alike, RT-09) is
  evaluated through the applicable hard controls (CHECK-01/02/04/06/11/16),
  including concentration changes caused by sell subsets. Hackathon
  proposals are small; exhaustive enumeration up to `MAX_ENUMERATED_LEGS`
  (12) is acceptable; beyond it the assessment fails closed.
* Buys: under the fixed investment-pool base, a single-leg fill never shows
  a fake 100% concentration; funding checks remain for subset buys.
* Sells: coverage is evaluated for every fill subset including the empty
  (zero-fill) subset; `zero_fill_covers_obligations=False` means liquidity
  must never be represented as restored until actual fills reconcile and
  settle. Coverage arithmetic conservatively assumes buy notional is spent.
* The assessment is wired into the authority decision (RT-06): UNSAFE →
  REJECT before any AUTO is reachable.

## Rebalance timing (RT-08, accepted as designed)

Cash-neutral same-day SELL→BUY rebalance is intentionally unavailable: sell
proceeds are T+1, so a REBALANCE may require Day 1 sell → T+1 settlement →
Day 2/next eligible session buy. Unsettled proceeds never simulate same-day
self-funding (CHECK-01/06/11 measure gross buy notional against settled
cash).

## Deterministic order identity (CHECK-09)

`client_order_id = "opaca-" + sha256(f"{proposal_id}:{leg_index}")[:32]` —
38 chars, alphanumeric/hyphen, within Alpaca limits verified in Phase −1B
(B3: hex IDs up to 128 chars accepted; duplicates rejected by broker).
Retrying a logical leg reproduces the same broker identifier.

## Calendar

`calendar/us_trading_calendar.py`: rule-based `USTradingCalendar` (weekday
rule + NYSE holiday/early-close tables, **supported range 2025-01-01 …
2027-12-31 only**, RT-03 — outside the range every lookup raises
`CalendarError`; weekdays are never extrapolated as sessions) and
`StaticTradingCalendar` (authoritative session list, e.g. Alpaca calendar
endpoint). `next_trading_day` is bounded by the calendar's last supported
date and raises `CalendarError` when no next session exists — no scan toward
`date.max`, no escaping `OverflowError` (RT-04). Tests assert the built-in
calendar matches Phase −1 evidence exactly for 2026-08-28 … 2026-10-12
(31 sessions; only missing weekday 2026-09-07 / Labor Day).

## Running the gates (offline)

Toolchain baseline is CPython **3.11** (red-team finding: mypy ≥ 2 dropped
the Python 3.9 checking target; the project baseline moved to 3.11). Exact
gate versions are pinned in `backend/requirements-dev.txt`:

| tool   | version |
| ------ | ------- |
| python | 3.11    |
| mypy   | 2.3.1   |
| pytest | 9.1.1   |
| ruff   | 0.16.5  |

```sh
cd backend
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest        # deterministic suite, no credentials
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy                    # strict
```
