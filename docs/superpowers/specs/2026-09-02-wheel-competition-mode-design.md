# Opaca Wheel Competition Mode — Design Specification

Date: 2026-09-02
Status: Approved design; implementation not started
Branch: `feat/wheel-competition-mode`
Baseline: `3b8e1ac6ab0c7a545b21e47dc84f85bad46f63c0`

## 1. Purpose

Build the smallest defensible Alpaca hackathon competition mode that adds real AI-assisted options trading without weakening the existing verified Opaca equity/treasury execution core.

Product thesis:

- **AI reasons. Software enforces. Alpaca executes.**
- **Capital is fully investable. Risk is not fully delegated.**
- **The AI can repair its idea. It cannot repair the policy.**

Competition capital is the reconciled cash already present in the dedicated Alpaca paper account. Corporate payroll, vendor obligations, and operating reserve are not subtracted in Competition Mode. Broker `buying_power`, `options_buying_power`, and equity are diagnostics for V1 and must never enlarge Opaca's internal capital authority.

## 2. Scope

### Must support for V1

1. Wheel strategy entry via one-contract cash-secured puts (CSPs).
2. Alpaca MCP as a strictly read-only AI/tool lane.
3. AI-generated structured `OptionIntent`.
4. One bounded AI repair attempt after deterministic rejection.
5. Deterministic contract selection from authoritative Alpaca data.
6. Deterministic Wheel risk policy.
7. `AUTO`, `APPROVAL_REQUIRED`, and `REJECT` authority semantics.
8. Atomic assignment-capital reservation before broker submission.
9. Options mutation through `alpaca-py` only, PAPER only.
10. Real option order read-back and Wheel reconciliation.
11. Sanitized execution evidence artifact.

### If time remains

- Covered-call opening path from reconciled shares.
- `BUY_TO_CLOSE` support.
- Activity-based secondary confirmation for expiry/assignment.
- Additional whitelisted underlyings.

### Explicitly out of scope before submission

- Naked short options.
- Spreads, iron condors, or multi-leg execution.
- Automatic rolling.
- Exercise automation.
- Black-Scholes, custom Greeks, volatility forecasting.
- RL, custom backtester, portfolio optimizer.
- Market-wide screener.
- Live-money mode.
- Multi-account support.
- CLI as a broker mutation surface.
- Generalizing the existing equity execution service into a universal asset-class executor.
- New web UI.

## 3. Architecture

```text
Alpaca MCP (READ ONLY)
        |
        v
AI Agent
        |
   OptionIntent
        |
        v
deterministic intent validator
        |
        v
alpaca-py authoritative contract + quote refresh
        |
        v
deterministic CSP selector
        |
        v
WheelGuard hard policy
        |
        +---- REJECT ----> one bounded AI repair attempt ----+
        |                                                   |
        +---- PASS ------------------------------------------+
        |
        v
existing authority semantics
REJECT / APPROVAL_REQUIRED / AUTO
        |
        v
atomic assignment-capital reservation
        |
        v
alpaca-py PAPER mutation
        |
        v
broker read-back
        |
        v
option-aware reconciliation
        |
        v
SHORT_PUT_OPEN / CASH / SHARES_HELD / UNKNOWN
```

The existing ETF/treasury execution path remains frozen and continues to use its verified `alpaca-py` implementation.

## 4. MCP and AI Boundary

Alpaca MCP is an observation/tool surface only. The AI must not receive mutation tools.

Allowed MCP capabilities must be limited to the minimum read-only toolsets needed for:

- permitted asset discovery;
- stock market data;
- options contracts/chains/quotes;
- news/context where useful.

Do not expose trading/order mutation, exercise, cancel/replace, account mutation, or other destructive tools to the AI.

MCP data informs the AI. It is not authoritative execution data.

Before policy evaluation and broker mutation, Opaca must re-read contract metadata and option quote data through its authoritative `alpaca-py` gateway.

## 5. OptionIntent

The AI emits an untrusted structured intent, not a broker order.

Minimum fields:

```text
action
underlying
market_view
thesis
willing_to_own_at_or_below
dte_preference
confidence
```

V1 permitted actions:

```text
SELL_CASH_SECURED_PUT
HOLD
```

The AI must not control or emit authoritative values for:

- OCC symbol;
- contract quantity;
- contract multiplier;
- final strike/expiration selection;
- limit premium;
- assignment capital;
- authority result;
- policy limits;
- whitelist membership.

An intent outside the permitted underlying whitelist fails validation.

## 6. AI Repair Loop

A policy rejection may be returned to the AI as structured feedback containing only violated checks and authoritative arithmetic.

Example:

```json
{
  "result": "REJECT",
  "violations": [
    {
      "code": "ASSIGNMENT_LIMIT_EXCEEDED",
      "proposed_assignment_capital": "32000.00",
      "maximum_assignment_capital": "25000.00"
    }
  ],
  "policy_is_not_editable": true
}
```

The AI may produce at most one repair intent.

```text
MAX_AGENT_ATTEMPTS = 2
```

Attempt 1 is the initial proposal. Attempt 2 is one repair. A second rejection terminates with no broker mutation.

The AI may never modify policy limits, capital, whitelist, multiplier, broker truth, or a hard `REJECT` result.

## 7. Underlying Universe

Use a tiny explicit whitelist, initially 3–5 liquid names that have active options and fit the account's assignment-capital constraints.

Do not implement a market-wide scanner.

The whitelist is policy, not AI-controlled. Final V1 symbols must be selected only after a read-only market/options probe verifies that usable contracts exist under the current assignment-capital limit.

## 8. Deterministic CSP Contract Selector

The selector maps a validated AI ownership preference to a real Alpaca-listed contract.

V1 selector requirements:

1. Underlying must be permitted.
2. Contract must be a PUT.
3. Contract must be active/tradable and unexpired.
4. DTE must be between 0 and 7 calendar days inclusive at selection time.
5. Strike must be less than or equal to `willing_to_own_at_or_below`.
6. Multiplier must be read from contract metadata and be positive; never hardcode `100` as authoritative.
7. Authoritative option quote must be fetched no more than 15 seconds before policy evaluation; a future source timestamp is invalid.
8. Bid must be positive and ask must not be below bid.
9. V1 opening quantity is exactly one contract.
10. Choose the eligible strike closest to the AI ownership price without exceeding it; then choose the earliest eligible expiration; final tie-break is lexical OCC symbol order.
11. V1 sell limit premium is the fresh authoritative bid. If that price is not broker-valid, fail closed rather than silently reprice.

The selector must not silently choose a safer strike merely to make policy pass. If the selected contract violates policy, WheelGuard rejects it and the AI may use its single repair attempt.

## 9. Capital Model

V1 uses one unambiguous internal capital base:

```text
capital_base = reconciled_cash
```

Broker equity, `buying_power`, and `options_buying_power` are diagnostics only. They cannot enlarge internal authority.

Every pending/open CSP assignment commitment is represented by one ACTIVE `CASH_DEPLOYMENT` reservation. Therefore:

```text
active_assignment_commitment
= sum(ACTIVE CSP CASH_DEPLOYMENT reservation amounts)

available_capital
= reconciled_cash - active_assignment_commitment
```

The same commitment must not be represented twice. Pending and filled/open CSP exposure use the same reservation lifecycle.

`options_buying_power` may be read as a broker feasibility/collateral diagnostic. If Opaca's internal model says a trade is feasible but broker collateral diagnostics disagree, fail closed.

## 10. CSP Risk Arithmetic

For a proposed CSP:

```text
assignment_capital
= strike × contract_multiplier × contracts
```

Premium received must not reduce the hard collateral/concentration gate.

Premium may be used for economics/reporting, e.g. adjusted entry cost, but not to make an unsafe assignment commitment appear smaller.

## 11. Hard Risk Policy

### Per-underlying concentration

```text
max_assignment_per_underlying
= 25% × reconciled_cash
```

At $100,000 reconciled cash, the hard maximum is $25,000 per underlying.

The per-underlying calculation includes existing ACTIVE assignment reservations for that underlying plus the proposed assignment commitment.

### Aggregate solvency

A proposed CSP must satisfy both:

```text
active_assignment_commitment + proposed_assignment_capital <= reconciled_cash
proposed_assignment_capital <= available_capital
```

These are equivalent under a consistent reservation ledger and are both checked for audit clarity.

### Broker feasibility

Broker collateral diagnostics must not contradict internal feasibility. Broker buying power must never be used to increase Opaca's limit.

### Other hard checks

- PAPER environment only.
- Kill switch clear.
- Valid permitted underlying.
- Valid real PUT contract.
- Positive integer contract quantity.
- Explicit valid multiplier.
- Fresh authoritative option quote.
- No unresolved/conflicting option order for the underlying.
- Wheel state permits CSP opening.
- Deterministic client-order identity/idempotency checks pass.
- UNKNOWN or contradictory broker state blocks new action.

Hard policy failures are `REJECT` and cannot be human-overridden.

## 12. Delegated Authority

Use the existing three-state authority semantics.

For a policy-valid CSP:

```text
assignment_capital <= 10% of reconciled_cash
    => AUTO

assignment_capital > 10% and <= 25% of reconciled_cash
    => APPROVAL_REQUIRED

assignment_capital > 25% of reconciled_cash
    => REJECT at hard policy stage
```

At $100,000 reconciled cash:

```text
<= $10,000       AUTO
> $10,000–$25,000 APPROVAL_REQUIRED
> $25,000        REJECT
```

Existing semantics remain mandatory:

- `REJECT` is never promoted by human approval.
- Human approval may only promote `APPROVAL_REQUIRED`, followed by fresh reconciliation, fresh contract/quote validation, and fresh policy evaluation before submission.

## 13. Domain Boundary

Options must use separate typed domain models rather than overloading the existing equity `ProposedOrder`.

Minimum concepts:

```text
OptionContract
  occ_symbol
  underlying
  right
  strike
  expiration
  multiplier

OptionQuote
  bid
  ask
  as_of

OptionIntent
  untrusted AI intent fields

OptionOrderRequest
  contract
  contracts:int
  action
  limit_premium

WheelPosition
  underlying
  shares:int
  short_put_occ_symbol | null
  short_put_contracts:int
  covered_call_occ_symbol | null
  covered_call_contracts:int
  status
  updated_at
```

Wheel status values:

```text
CASH
SHORT_PUT_OPEN
SHARES_HELD
COVERED_CALL_OPEN
UNKNOWN
```

Status is a derived convenience label. Reconciled broker quantities/identities are the underlying truth.

The V1 strategy submits only one opening CSP contract at a time, but the data model must not prevent future representation of partial assignment.

## 14. Policy Output Vocabulary

Keep one audit/policy vocabulary.

Do not create a parallel `OptionRiskAssessment` rendering/persistence system.

WheelGuard must emit the existing policy-result style (`PolicyDecision` / `PolicyCheckResult` / `CheckId`) with option-specific checks.

V1 option checks:

```text
CHECK-17 WHEEL_STATE
CHECK-18 OPTION_CONTRACT
CHECK-19 OPTION_QUOTE
CHECK-20 ASSIGNMENT_CAPITAL
```

AUTO vs APPROVAL_REQUIRED belongs in the authority layer, not a hard-policy `CHECK-21`.

## 15. Broker and Execution Boundary

Existing ETF execution remains unchanged.

New Wheel option mutation must use `alpaca-py`, not CLI.

Do not generalize the existing large equity execution service. Implement a slim option execution path beside it that reuses the proven lifecycle pattern:

```text
reconcile
-> evaluate
-> authority
-> reserve atomically
-> persist intent
-> submit
-> broker read-back
-> reconcile
```

The existing paper-endpoint structural verification, kill switch, deterministic client-order ID, unresolved-order protections, audit discipline, and fail-closed recovery should be reused where their semantics remain correct.

## 16. Reservation Lifecycle

Before option mutation, create an ACTIVE assignment-capital reservation under a `BEGIN IMMEDIATE` transaction.

For a $160 strike, 100 multiplier, one-contract CSP:

```text
reservation kind = CASH_DEPLOYMENT
symbol = underlying
amount = 16000.00
status = ACTIVE
```

Lifecycle:

```text
CSP pending/open/filled -> reservation ACTIVE
put expires worthless -> reservation RELEASED
put assigned -> reservation RELEASED once the contingent obligation becomes actual broker cash/share state
```

If broker submission or read-back is ambiguous, keep the reservation ACTIVE until broker truth proves that no exposure exists.

Never release collateral solely because a submission call timed out.

## 17. Persistence Isolation

Use a fresh Competition Wheel database rather than upgrading or overwriting the existing verified treasury/equity persisted state in place.

Conceptual file:

```text
opaca-wheel-paper.db
```

Reuse the persistence architecture:

- SQLite WAL;
- foreign keys;
- `BEGIN IMMEDIATE` single-writer discipline;
- audit events;
- reservations;
- deterministic order identity;
- fail-closed startup.

Do not reuse the old scenario/obligation state as Competition Mode capital policy.

## 18. Option Broker Adaptation

Do not weaken the existing equity `adapt_position()` semantics just so short options fit its `Position` model.

Add option-specific adapters and records, e.g.:

```text
adapt_option_position()
adapt_option_order()
adapt_option_activity()
```

A broker short option is valid Wheel state; it must not be interpreted as an illegal naked equity short.

Option market data is separate from the existing IEX stock quote path. Reuse generic freshness principles, not the stock-specific market adapter.

## 19. Reconciliation

The first successful CSP fill must reconcile immediately without waiting for expiry.

Expected post-fill state includes:

```text
local authorized option order
broker order read-back = filled/open as applicable
broker short option position for exact OCC symbol
active assignment-capital reservation
WheelPosition = SHORT_PUT_OPEN
reconciliation = RECONCILED
```

### Expiry and assignment evidence

Primary evidence:

- option position state;
- underlying share delta;
- broker cash delta;
- order state;
- time relative to expiration.

Secondary confirmation:

- broker option activities when available.

Do not require delayed activity data to unblock a state transition if positions/cash/order evidence yields one unambiguous hypothesis. Later activity must contradiction-check the earlier conclusion.

### PUT expires worthless

Transition `SHORT_PUT_OPEN -> CASH` only when the exact option position is absent, underlying shares are unchanged, relevant order state is terminal, the expiration safety buffer has elapsed, and the evidence is otherwise consistent.

### PUT assigned

Transition `SHORT_PUT_OPEN -> SHARES_HELD` when the exact option position is absent, underlying shares increase by an exact contract-multiplier quantity, broker cash moves consistently with strike × multiplier × assigned contracts within defined tolerance, and no conflicting hypothesis exists.

The reservation is released when assignment is established because the contingent collateral has become actual broker cash/share state.

## 20. UNKNOWN Semantics

Per-underlying `UNKNOWN` blocks new Wheel action for that underlying.

Examples:

- broker read unavailable;
- malformed option position;
- missing/invalid multiplier;
- unresolved option order;
- local option record with no explainable broker counterpart;
- broker option position with no local record;
- inconsistent share delta;
- materially inconsistent cash delta;
- more than one plausible expiry/assignment hypothesis;
- later broker activity contradicts the inferred transition.

If uncertainty affects global cash, aggregate collateral, broker availability, or account-wide reconciliation, block all new Wheel opening trades.

Do not silently rewrite historical state after contradictory broker evidence.

## 21. Covered Call Extension

Covered calls are part of the Wheel product but are not required for the first eligibility-critical implementation milestone.

If implemented before submission, minimum safety checks are:

1. Reconciled `SHARES_HELD` state for the underlying.
2. Exact CALL contract identity and matching underlying.
3. `contracts × multiplier <= reconciled available shares` after subtracting existing share reservations/open covered calls.
4. Fresh authoritative option quote.
5. No unresolved/conflicting option order.
6. Paper/kill-switch/idempotency checks pass.

Use existing `SELL_QUANTITY` reservation semantics for covered shares.

Strike above cost basis may be a soft economics preference, not a hard safety requirement.

## 22. Evidence Artifact

After a real CSP order is successfully reconciled, produce a sanitized machine-readable artifact containing at least:

```text
mode
environment
underlying
occ_symbol
action
contracts
strike
multiplier
assignment_capital
authority result
broker_order_id
client_order_id
broker status
wheel state
reserved assignment capital
reconciliation result
```

Never persist or emit API keys, secrets, tokens, or a full account number.

## 23. Demo Hero Flow

Target judge flow:

```text
AI uses read-only Alpaca MCP context
-> proposes CSP ownership intent
-> authoritative selector maps it to a real contract
-> assignment exposure = $32,000
-> per-underlying limit = $25,000
-> REJECT with visible arithmetic
-> AI receives the violated check
-> one repair intent
-> assignment exposure = $16,000
-> hard policy PASS
-> authority decision
-> if approval needed, approve then fully revalidate
-> alpaca-py PAPER submission
-> real broker fill/read-back
-> SHORT_PUT_OPEN reconciled
-> committed assignment capital shown
```

The demo must end on real broker evidence rather than a hypothetical proposal.

## 24. Specification Amendment Requirement

Implementation must add a narrowly scoped amendment to the main Opaca SPEC documenting that Alpaca MCP is permitted as a non-authoritative, read-only AI context/tool lane for Competition Mode.

The amendment must preserve the existing principle that broker mutation and authoritative adjudication remain inside Opaca's deterministic `alpaca-py` boundary.

CLI must remain outside the runtime mutation path.

## 25. Testing and Implementation Discipline

All production changes follow strict TDD:

1. Write a focused failing test.
2. Run it and observe the expected RED failure.
3. Implement the minimum production change.
4. Re-run and observe GREEN.
5. Run relevant regression gates before each logical milestone.

No production code may be written before its intended failing test is observed.

Never use a real broker mutation merely to make a test pass. Real PAPER mutation is a distinct final proof step after the deterministic/local suite is green.

## 26. Implementation Priority

Absolute order:

1. Option-safe broker adaptation/reconciliation foundation.
2. Option domain models + contract/quote read boundaries.
3. Wheel hard-policy checks + assignment reservations.
4. Slim `alpaca-py` option execution path.
5. MCP read-only AI intent lane + one repair attempt.
6. Delegated authority wiring.
7. One real short-dated CSP PAPER proof and sanitized artifact.
8. README/demo script/submission materials.
9. Covered call only if time remains.

Do not start UI, optimizer, Greeks, spreads, or broad red-team work before these are complete.
