# Opaca Wheel Competition Mode — Final Review Amendment

Date: 2026-09-02
Status: Approved design amendment; implementation not started
Applies to: `docs/superpowers/specs/2026-09-02-wheel-competition-mode-design.md`
Baseline: `3b8e1ac6ab0c7a545b21e47dc84f85bad46f63c0`

This amendment closes the final pre-implementation review blockers. It does **not** change the approved architecture. Where this amendment conflicts with the original Wheel Competition Mode design specification, **this amendment wins**. Unchanged sections of the original specification remain in force.

## A. Capital Definitions — supersedes ambiguous capital language in §§1, 9, 11, 12

Competition Mode uses two different cash concepts deliberately:

```text
risk_capital_base
= immutable opening competition cash

reconciled_cash
= current Alpaca account.cash from the latest successful Competition Mode reconciliation snapshot
```

### A1. `reconciled_cash`

`reconciled_cash` is the broker-reported Alpaca `account.cash` field adapted into `BrokerCashState.cash`. It is captured as one successful Competition Mode reconciliation snapshot and persisted before policy evaluation. Competition Mode does not add derived settlement credits to it. If broker cash timing is ambiguous during assignment/expiry reconciliation, the affected state is `UNKNOWN` rather than guessed.

### A2. `risk_capital_base`

`risk_capital_base` is seeded exactly once from the first clean, PAPER, RECONCILED competition-account snapshot **before any Wheel mutation** and is immutable for the life of that Wheel database.

Premium credits, assignment, mark-to-market changes, losses, gains, and later deposits must not silently increase `risk_capital_base`. A deliberate reset requires a new Wheel database.

At the intended competition starting state this is approximately USD 100,000, but policy must use the persisted seeded value rather than a hardcoded display number.

Broker equity, `buying_power`, and `options_buying_power` are diagnostics only. They never enlarge `risk_capital_base` or a delegated-authority ceiling.

## B. Current Cash, Reservations, and Held-Share Exposure — supersedes §§9 and 11 arithmetic

Every pending/open CSP assignment commitment is represented by **exactly one** ACTIVE `CASH_DEPLOYMENT` reservation.

```text
active_assignment_commitment
= sum(ACTIVE CSP CASH_DEPLOYMENT reservation amounts)

available_cash
= reconciled_cash - active_assignment_commitment
```

The same logical CSP must never have separate “pending” and “filled” assignment reservations. It keeps the same reservation through pending/open/filled states until expiry, assignment, or a broker-proven terminal unfilled outcome.

If `available_cash < 0`, fail closed.

### B1. Assigned shares continue to consume concentration capacity

Assignment converts a contingent cash commitment into owned shares. It does **not** remove exposure from Wheel policy.

In the same reconciled transition that releases the CSP assignment reservation, Opaca must create/update an attributable Wheel share lot.

For each underlying:

```text
wheel_share_cost_basis
= remaining Wheel-attributable shares × persisted assignment basis

reconciled_share_market_value
= authoritative broker market value of those reconciled Wheel-attributable shares

held_share_exposure
= max(wheel_share_cost_basis, reconciled_share_market_value)
```

If Wheel-attributable shares cannot be distinguished from unexplained/manual shares, or authoritative basis/market value cannot be established, that underlying becomes `UNKNOWN` and cannot open another CSP.

```text
underlying_wheel_exposure
= held_share_exposure
+ ACTIVE assignment reservations for that underlying

aggregate_wheel_exposure
= sum(held_share_exposure across underlyings)
+ active_assignment_commitment
```

Held shares count toward concentration and delegated authority after assignment. They are not subtracted from `reconciled_cash` a second time because the broker cash debit already occurred.

## C. Hard Wheel Exposure Policy — supersedes §§11–12 where inconsistent

For a proposed CSP:

```text
proposed_assignment_capital
= strike × authoritative contract multiplier × contracts

post_trade_underlying_exposure
= current held_share_exposure for underlying
+ current ACTIVE assignment reservations for underlying
+ proposed_assignment_capital

post_trade_aggregate_wheel_exposure
= aggregate_wheel_exposure
+ proposed_assignment_capital
```

Hard requirements:

```text
post_trade_underlying_exposure <= 25% × risk_capital_base
post_trade_aggregate_wheel_exposure <= risk_capital_base
proposed_assignment_capital <= available_cash
```

Premium received does not reduce any numerator and does not increase `risk_capital_base`.

Broker options-collateral diagnostics must not contradict internal feasibility. Broker buying power must never be used to increase an Opaca limit. Missing or contradictory options-collateral diagnostics fail closed for a new CSP.

## D. Delegated AUTO Envelope — supersedes §12

The Competition Mode AUTO envelope is deliberate: the agent may trade autonomously only inside a small pre-authorized exposure envelope. Hard policy is never overridable.

For a **policy-valid** CSP, AUTO is allowed only if all three are true:

```text
proposed_assignment_capital <= 10% × risk_capital_base
post_trade_underlying_exposure <= 10% × risk_capital_base
post_trade_aggregate_wheel_exposure <= 20% × risk_capital_base
```

If hard policy passes but any AUTO condition fails:

```text
APPROVAL_REQUIRED
```

If any hard policy rule fails:

```text
REJECT
```

Therefore a previously human-approved larger exposure prevents further AUTO openings while aggregate/per-name exposure remains outside the delegated envelope.

Any existing stricter rolling-notional, rolling-count, or runaway controls remain additional constraints and cannot widen this envelope.

## E. Human Approval TTL and Final Revalidation — extends §12

A Wheel approval expires after **5 minutes** and binds:

- `wheel_decision_run_id`;
- attempt number;
- exact OCC symbol;
- action;
- contract count;
- assignment capital;
- approved sell limit premium.

Before submission after approval, Opaca must obtain fresh reconciliation and a fresh authoritative option quote no older than 15 seconds and rerun all hard policy checks against current ACTIVE reservations.

Contract identity, multiplier, account binding, and assignment arithmetic must be unchanged. If the fresh bid is below the approved limit premium, or any bound field changes, the approval is stale and the order must not be submitted.

Immediately before **any** broker submission, AUTO or approved, the authoritative quote must still be no older than 15 seconds and the current bid must be at least the persisted sell limit premium. Otherwise restart from fresh quote/policy evaluation.

## F. MCP Read-Only Enforcement — supersedes §4

MCP read-only is an enforced startup invariant, not a prompt instruction.

After the read-only feasibility probe identifies the installed Alpaca MCP tool names, V1 must commit an exact `MCP_ALLOWED_TOOLS` allowlist.

At agent-runner startup:

```text
required_read_tools <= exposed_tools
exposed_tools <= MCP_ALLOWED_TOOLS
```

If any exposed tool is not allowlisted, startup fails closed **before the model is invoked**.

The allowlist must exclude all order mutation, exercise, cancel/replace, account mutation, and other destructive tools. Tests must inject at least one mutation-style tool name such as `place_option_order` and prove startup is blocked.

MCP observations can never satisfy a hard policy check. Contract metadata, option quotes, account state, positions, and collateral diagnostics used for policy/execution must be re-read independently through authoritative `alpaca-py` paths.

## G. AI Repair Budget — clarifies §6

Repair attempts are scoped to one persisted `wheel_decision_run_id`:

```text
MAX_AGENT_ATTEMPTS_PER_RUN = 2
```

Attempt 1 is the initial intent. Attempt 2 is the only repair. A second rejection terminates that run with no broker mutation.

The runner must not recursively start a fresh run merely to evade the attempt limit. A new decision run begins only after fresh reconciliation. An underlying with an open, unresolved, approval-pending, or `UNKNOWN` Wheel state cannot start a fresh CSP decision run.

## H. Deterministic V1 Selector Constants — supersedes/clarifies §8

V1 selector rules:

1. PUT only.
2. Underlying must be in the explicit whitelist.
3. Active/tradable and unexpired contract only.
4. DTE is **1–7 calendar days inclusive**. 0-DTE opening is forbidden.
5. No new CSP opening in the final **30 minutes before regular market close**.
6. Strike <= AI `willing_to_own_at_or_below`.
7. Authoritative positive multiplier from contract metadata; never assume 100.
8. Authoritative quote <=15 seconds old; future source timestamp invalid.
9. `bid > 0` and `ask >= bid`.
10. Exactly one opening contract in V1.
11. Minimum premium yield:

```text
premium_yield_on_assignment
= (bid × multiplier × contracts) / proposed_assignment_capital

MIN_PREMIUM_YIELD_ON_ASSIGNMENT = 0.001  # 0.10%
```

12. Choose strike closest to ownership price without exceeding it, then earliest eligible expiration, then lexical OCC-symbol tie-break.
13. Sell limit premium = authoritative bid at selection/final validation.
14. Time-in-force = `DAY` only.

The selector must never silently move to a safer strike merely to make policy pass. A policy failure is returned to the AI repair loop.

## I. Option Order Identity — extends §15

Do not reuse the equity logical identity input (`proposal_id + leg_index`) without defining the option logical order.

The V1 option client-order ID must hash a canonical serialization containing at least:

```text
wheel_decision_run_id
attempt_number
OCC symbol
action
contracts
limit premium
```

Retries of the **same logical submission** reuse the same client-order ID. A changed attempt, contract, action, quantity, or limit is a different logical order and receives a different ID.

## J. Atomic Final Check and Reservation — clarifies §15

The broker read itself need not hold the SQLite write lock. The mutation boundary is:

```text
fresh broker reconciliation / quote
-> policy / authority
-> BEGIN IMMEDIATE
-> assert exact snapshot version and account binding are still current
-> recompute final reservation-dependent exposure and available-cash checks from ACTIVE DB rows
-> create assignment reservation
-> persist logical order identity / intent
-> COMMIT
-> final <=15s quote check
-> submit through alpaca-py PAPER
```

If snapshot version, account fingerprint, reservation totals, or final quote validity changed, rollback or do not submit and restart from reconciliation. The read-check-reserve operation must not allow two callers to spend the same unreserved cash.

## K. Dedicated Competition Account Binding — extends §17

At fresh Wheel DB bootstrap, after a clean PAPER reconciliation and before any mutation, persist:

```text
account_fingerprint = SHA-256(full broker account id)
risk_capital_base = opening BrokerCashState.cash
```

Persist the hash, not the full account id/account number in user-facing artifacts.

Every startup and every mutation boundary must compare the current broker account fingerprint with the DB binding. Mismatch is a hard failure and blocks all Wheel mutations.

Bootstrap must fail closed if the account already contains unexplained option positions, unresolved orders, or holdings that cannot be reconciled into the new Wheel state. Do not silently adopt another strategy's positions.

## L. Order Lifecycle and Reservation Release — supersedes §16 gaps

V1 opening CSP orders use `DAY` time-in-force.

One logical CSP keeps one ACTIVE assignment reservation while it might still create or represent short-PUT exposure.

```text
pending / submitted / accepted / new
    -> reservation ACTIVE

UNKNOWN / timeout / ambiguous read-back / cancel-pending
    -> reservation ACTIVE

broker-confirmed terminal with filled contracts = 0
AND exact OCC position absent
AND no unresolved order with same client_order_id
    -> reservation RELEASED

FILLED and exact short PUT position exists
    -> reservation ACTIVE

assignment proven
    -> create/update attributable Wheel share exposure
       THEN release assignment reservation in the same reconciled transition

expiry worthless proven
    -> reservation RELEASED
```

Broker-confirmed terminal no-exposure states include rejected, cancelled, broker-expired/day-ended orders, and proven NOT_SUBMITTED. A timeout alone is never terminal and never releases capacity.

## M. Policy Check Mapping — supersedes §14

Reuse existing checks where their semantics already match:

```text
CHECK-00  kill switch
CHECK-08  PAPER/environment verification
CHECK-09  deterministic client-order identity/idempotency
CHECK-13  runaway autonomous-order control
```

Add:

```text
CHECK-17  WHEEL_STATE
CHECK-18  OPTION_CONTRACT
CHECK-19  OPTION_QUOTE_ECONOMICS
CHECK-20  WHEEL_EXPOSURE_CASH
CHECK-21  BROKER_OPTION_COLLATERAL
CHECK-22  COMPETITION_ACCOUNT_BINDING
```

`CHECK-18` owns whitelist, PUT/right, tradability, 1–7 DTE, opening blackout, integer contracts, multiplier.

`CHECK-19` owns quote freshness/future timestamp, bid/ask validity, minimum premium yield, broker-valid DAY limit.

`CHECK-20` owns per-underlying hard exposure, aggregate hard exposure, and current available-cash feasibility.

AUTO vs APPROVAL_REQUIRED remains an authority-layer decision, not CHECK-20/21/22.

MCP tool-surface enforcement is a pre-agent startup gate, not a WheelGuard CheckId.

## N. Reconciliation Triggers and Constants — extends §§19–20

Immediate post-fill `RECONCILED` requires all of:

```text
local authorized option order
broker read-back for exact client_order_id and OCC symbol
matching broker short PUT position and contract quantity
ACTIVE assignment reservation equal to proposed assignment capital
WheelPosition = SHORT_PUT_OPEN
unchanged account fingerprint
```

A filled order without the matching short-option position, or a position without matching local order/reservation, is not RECONCILED.

Reconciliation is mandatory:

1. at Wheel runner startup;
2. before every new AI decision run;
3. immediately after submit/read-back and any observed order-state change;
4. before releasing/using an assignment reservation;
5. on explicit `wheel-reconcile` invocation;
6. on the first run after expiration before any next Wheel action.

V1 constants:

```text
EXPIRATION_RECONCILIATION_BUFFER
= until 09:35 America/New_York on the next regular trading session after expiration

ASSIGNMENT_CASH_TOLERANCE
= max($5.00, 0.0005 × proposed assignment capital)
```

A materially inconsistent assignment cash delta means a delta outside `ASSIGNMENT_CASH_TOLERANCE` after accounting only for broker-reported fills/fees explicitly present in the evidence set.

Expiry worthless is proven only after the buffer when the exact option position is absent, underlying shares are unchanged, the relevant order is terminal, and no assignment evidence exists.

Assignment is proven only when the option position is absent, underlying shares increase by an exact multiplier quantity, cash movement is consistent within tolerance, and no conflicting hypothesis exists.

If assignment is proven, create/update the attributable share lot before releasing the CSP reservation so concentration exposure is never lost from the ledger.

Any contradiction, unexplained/manual shares, missing broker identity, ambiguous cash/share delta, unresolved order, or conflicting later activity produces `UNKNOWN`. Per-underlying UNKNOWN blocks that name; account/cash/collateral/account-binding uncertainty blocks all new Wheel openings.

## O. Required Tests Before the First Real CSP — extends §25

Before any real option mutation, RED→GREEN tests must prove at least:

- `risk_capital_base` seeds once and premium/current-cash changes cannot enlarge it;
- assigned shares remain in concentration/aggregate exposure after reservation release;
- AUTO cannot exceed 10% per-name or 20% aggregate post-trade exposure;
- policy-valid exposure above the AUTO envelope becomes APPROVAL_REQUIRED;
- broker-confirmed terminal zero-fill orders release reservation only after absence of exposure is proven;
- timeout/UNKNOWN retains reservation and suppresses duplicate retry;
- MCP startup rejects a non-allowlisted mutation tool;
- account fingerprint mismatch blocks mutation;
- final reservation-dependent checks occur inside the same `BEGIN IMMEDIATE` transaction that creates the reservation;
- approval TTL and final quote/binding validation fail closed when stale;
- option client-order ID is stable for the same logical retry and changes for a different logical order.

No real paper mutation may be used to make these tests pass.

## P. Updated Implementation Order — supersedes §26

1. Read-only feasibility probe: competition PAPER account, actual account/options-collateral fields, option contract multiplier, quote timestamp shape, and exact MCP read-only tool names. No mutation.
2. Fresh Wheel DB bootstrap + hashed account binding + immutable `risk_capital_base`.
3. Option domain models + option-safe broker adapters/read gateway.
4. Wheel exposure accounting: assignment reservations + attributable held-share exposure.
5. WheelGuard hard checks + delegated authority arithmetic.
6. Option order lifecycle + deterministic option identity + approval TTL + atomic reservation transaction.
7. Post-fill option reconciliation.
8. Slim `alpaca-py` PAPER option execution path.
9. MCP read-only AI intent lane + enforced tool allowlist + one bounded repair attempt.
10. Sanitized evidence artifact.
11. One real short-dated CSP PAPER proof **only after all prerequisite safety tests are GREEN**.
12. README/demo/submission materials.
13. Covered call only if time remains.

Do not start UI, optimizer, Greeks, spreads, a new broad red-team round, or a second real options mutation before the above submission-critical path is complete.
