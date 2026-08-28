# Opaca v0.2 (as amended 2026-08-25)

## Autonomous Corporate Cash Agent powered by Alpaca


**Status:** FROZEN FOR BUILD — AMENDED (Amendments A–F applied)
**Supersedes:** Opaca v0.1 / TreasuryGuard initial spec
**Build window:** 28 August–4 September 2026
**Implementation starts:** Hackathon kickoff, subject to final official rules


---

## Amendment Log (A–D approved 2026-08-25 pre-build; E–F approved 2026-08-28 at kickoff)


| ID | Subject | Sections touched |
| -- | ------- | ---------------- |
| A | Paper account reset and demo baseline | §3, §4, §16, §17, §19, §21 |
| B | Settlement is an Opaca-derived liquidity model | §5, §9 (CHECK-12), §17, §21 |
| C | LLM contract and failure behavior | §7, §11, §20, §21 |
| D | Define target_weight exactly | §7, §8, §21 |
| E | Optional read-only Alpaca MCP context lane | §14, §17, §18, §21 |
| F | Alpaca CLI operational runbook (external tool only) | §14, §17, §18, §21 |
| — | Smaller clarifications | §4, §5, §9, §10, §13, §15, §17 |
| — | UNKNOWN recovery correction | §13, §20, §21 |
| — | CHECK-16 no short positions (evidence-driven, Phase −1A: `shorting_enabled: true` observed) | §9, §21 |


### Amendment E


Optional read-only Alpaca MCP context lane, feature-flagged, disabled by default,
post-core only, no write tools, advisory context only.


### Amendment F


Alpaca CLI is an external operations/fault-injection tool only and is never invoked
by Opaca runtime.


All amendments preserve the existing architecture and the Freeze Rule (§23).

---


# 1. Product Definition


Opaca is an autonomous corporate cash agent that:


1. reads the company's real Alpaca paper-account cash and positions,
2. combines that state with known corporate obligations,
3. calculates protected and investable cash deterministically,
4. reasons about appropriate **liquidity horizon and duration exposure**,
5. produces a structured allocation or liquidation proposal,
6. passes every proposal through the deterministic **TreasuryGuard Policy Engine**,
7. auto-executes routine actions inside delegated authority,
8. escalates exceptional actions to a human,
9. executes through Alpaca paper trading,
10. reconciles fills, positions, settlement and liquidity,
11. continues monitoring for new events.


Core principle:


> **AI reasons. Software enforces. Alpaca executes.**


Autonomy principle:


> **Autonomous when policy allows. Human when judgment matters.**


---


# 2. What Makes the AI Necessary


The LLM does **not** perform arithmetic or choose arbitrary ETF percentages.


Its primary reasoning task is:


## Liquidity-aware maturity matching


The agent considers:


* when cash is needed,
* how certain those obligations are,
* how much liquidity headroom exists,
* existing duration exposure,
* permitted instruments,
* current market context.


It answers:


> **What duration can each available cash tranche safely tolerate?**


It may decide:


* invest,
* shorten duration,
* extend duration,
* rebalance,
* liquidate,
* or **do nothing**.


The agent must not manufacture differences between nearly identical instruments merely to justify a trade.


---


# 3. Single Cash Ledger


There is only one authoritative cash balance.


## Authoritative source


**Alpaca paper account cash**


$500,000 is the **preferred** demo starting balance, not an assumed invariant.


If the paper account cannot be reliably configured or reset to $500,000 (see §16, §17):


* Opaca does **not** create a synthetic second cash balance;
* actual Alpaca cash becomes the scenario base;
* seeded obligations and reserves are deterministically scaled from that cash while preserving the intended economics and ratios;
* the resulting demo values are documented.


SQLite stores:


* obligations,
* internal policies,
* forecasts,
* proposals,
* approvals,
* audit events.


SQLite does **not** invent another corporate cash balance.


Invariant:


> **Opaca never claims cash that Alpaca cannot reconcile.**


---


# 4. Corporate Liability Layer


Example baseline (**illustrative; assumes the $500,000 baseline was established — see §3, §16**):


| Item                      |   Amount | Due        |
| ------------------------- | -------: | ---------- |
| Alpaca cash               | $500,000 | Current    |
| Payroll                   | $120,000 | +10 days   |
| Suppliers                 |  $70,000 | +18 days   |
| Minimum operating reserve | $200,000 | Continuous |


Therefore:


```text
500,000
- 120,000
- 70,000
- 200,000
= 110,000 investable
```


The calculation is deterministic.


## Obligation dates (Amendment: smaller clarifications)


Obligations are stored as **explicit ISO dates** (`due_date`), never as relative "+N days" offsets.
Relative offsets exist only in scenario seeding: the demo reset (§16) computes concrete ISO dates
relative to the reset time. Due-date and settlement arithmetic uses the US securities
business-day calendar (weekends and US market holidays handled explicitly).


---


# 5. Liquidity Model


Opaca must distinguish:


### Settled Cash


Immediately usable corporate liquidity.


### Unsettled Sale Proceeds


Cash expected to settle on a known settlement date.


### Investments


Current market value of positions.


### Obligations


Known dated liabilities.


A sale does **not** automatically mean an obligation is funded.


Liquidity coverage must evaluate:


```text
settled_cash_available_by_due_date
+
proceeds_settling_before_due_date
-
obligations_due_by_that_date
```


## Settlement is an Opaca-derived liquidity model (Amendment B)


Do **not** rely on Alpaca paper-account cash behavior to represent legally/operationally
settled corporate liquidity.


Alpaca broker state remains authoritative for:


* orders,
* fills,
* positions,
* broker cash/account state.


Opaca deterministically derives a **settlement availability schedule** from actual reconciled
fills. For US ETF sales in the MVP, the expected settlement date is computed using the
applicable **T+1 US securities business-day calendar**, including weekend/holiday rolls.


Conceptually maintained:


* settled / available-by-date cash,
* unsettled proceeds with expected settlement date,
* investment market value,
* dated obligations.


This is **not a second cash ledger**. It is a derived availability schedule computed over
reconciled broker transactions, anchored to the single authoritative Alpaca balance (§3).
It does not violate the single-ledger invariant.


CHECK-12 evaluates against this derived schedule, **regardless of whether Alpaca paper
trading visually credits sale proceeds instantly** (see §9, §17).


---


# 6. Investment Universe


Initial universe:


* SGOV
* BIL
* SHV


Optional only after core completion:


* one genuinely longer-duration Treasury ETF such as SHY


A longer instrument may only be added if doing so materially improves maturity-matching behaviour and has an explicit duration/exposure policy.


No options.


No crypto.


No speculative equity strategies.


---


# 7. Agent Output Contract


Agent returns:


```json
{
  "decision": "allocate",
  "allocations": [
    {
      "symbol": "SGOV",
      "target_weight": 0.70,
      "horizon_bucket": "0_30_days",
      "rationale": "..."
    },
    {
      "symbol": "BIL",
      "target_weight": 0.30,
      "horizon_bucket": "31_90_days",
      "rationale": "..."
    }
  ],
  "summary": "..."
}
```


Allowed decisions (closed enum):


* `allocate`
* `rebalance`
* `liquidate`
* `hold`


The agent does **not** output:


* dollar order amounts,
* share quantities,
* limit prices,
* client order IDs,
* policy status,
* execution authority,
* `confidence`,
* trigger type.


Those are deterministic system responsibilities.


## Strict schema validation (Amendment C)


Agent output must pass strict schema validation before any downstream processing:


* required fields only,
* explicit enums,
* bounded numeric ranges (e.g. `target_weight ∈ (0, 1]`),
* no unknown/extra fields (unknown fields cause rejection, not silent dropping),
* no dollar amounts, share quantities, order IDs, or policy decisions anywhere in the payload.


Validation failure is an LLM failure (see §11), not a policy rejection.


## target_weight semantics (Amendment D)


The meaning of `target_weight` depends on the decision and must **never** be inferred implicitly:


| Decision     | Denominator                                                                                     |
| ------------ | ----------------------------------------------------------------------------------------------- |
| `allocate`   | Fraction of the deterministic deployable/investable budget assigned to that symbol.             |
| `liquidate`  | Fraction of the deterministic required liquidation amount sourced from that symbol.              |
| `rebalance`  | Desired projected post-trade weight within total invested market value.                          |
| `hold`       | `allocations` MUST be empty.                                                                     |


Examples:


* `allocate`, deployable budget $100k, weight 0.60 → code may budget up to $60,000 before share/price rounding.
* `liquidate`: the cash/liquidity engine first calculates the deterministic required liquidation amount. The LLM chooses only the **composition** (which symbols, in what fractions); it does **not** determine how much cash must be raised.
* `rebalance`: deterministic code calculates required trade deltas from reconciled current positions and the desired post-trade weights.


Schema invariants enforce these meanings (per-decision validation and tests) so a weight
cannot silently change meaning between decision types.


---


# 8. Deterministic Order Construction


Code converts agent intent to orders. The input to conversion depends on decision type
(Amendment D):


```text
allocate:   target weight → authorized investment budget → dollar allocation
rebalance:  desired post-trade weights → deltas vs reconciled positions → trade deltas
liquidate:  required liquidation amount (engine-computed) × composition weights → sell amounts
            ↓
            current quote → share quantity → limit price → residual cash
```


Weight-sum invariants enforced by validation:


* `allocate`: `sum(target_weight) <= 1.0`; any remainder remains cash.
* `liquidate`: `sum(target_weight) <= 1.0`; any remainder stays in the source holdings.
* `rebalance`: `sum(desired_weights) <= 1.0`.
* `hold`: `allocations == []`.


Rounding must never increase the intended budget.


Residual cash remains cash and is displayed honestly.


---


# 9. TreasuryGuard Policy Engine


## CHECK-00 — Kill Switch


If active:


```text
NO NEW ORDER MAY BE SUBMITTED
```


Checked immediately before every broker submission.


---


## CHECK-01 — Investable Cash


Proposed deployment cannot exceed deterministically calculated investable cash.


---


## CHECK-02 — Minimum Liquidity


Projected liquidity cannot breach the required operating reserve.


---


## CHECK-03 — Permitted Security


Every instrument must exist on the policy whitelist.


---


## CHECK-04 — Concentration


Concentration is calculated on:


> **projected post-trade total invested market value**


including:


* existing holdings,
* all proposed legs,
* expected post-trade positions.


Never calculate concentration on proposal amounts alone.


---


## CHECK-05 — Alpaca Tradability


The asset must be confirmed tradable through Alpaca.


---


## CHECK-06 — Cash Funding


Do not authorize trades from generic broker `buying_power`.


Funding must be constrained by actual permitted cash.


Broker leverage is not corporate liquidity.


---


## CHECK-07 — Autonomous Authority


AUTO requires all limits to pass:


```text
per-order notional
AND
per-proposal aggregate notional
AND
rolling 24-hour autonomous notional
AND
rolling autonomous order count
```


No limit may be bypassed by order splitting.


---


## CHECK-08 — Paper Environment


Execution must fail closed unless paper status is verified.


Verify both:


* configured endpoint/environment,
* broker account response/state where available.


Live credentials must not be usable by the hackathon build.


---


## CHECK-09 — Duplicate Execution


Every proposal leg has deterministic identity.


```text
client_order_id =
hash(proposal_id + leg_index)
```


The deterministic ID encoding must satisfy the actual Alpaca `client_order_id` constraints
(format, charset, maximum length) determined in Phase −1 (§17).


Database uniqueness:


```text
UNIQUE(proposal_id, leg_index)
UNIQUE(client_order_id)
```


Retrying the same logical leg creates the **same** broker identifier.


---


## CHECK-10 — Opposing Orders


Prevent simultaneous or logically conflicting opposing orders
(e.g. `BUY SGOV` / `SELL SGOV`) across **all unresolved proposals with overlapping symbols**.


There is no undefined "session" scope: the guard applies globally to every unresolved
proposal touching the same symbol.


---


## CHECK-11 — No Leverage


Opaca must never intentionally consume margin leverage.


Fail closed if the application cannot determine whether a proposed transaction depends on leverage.


---


## CHECK-12 — Settlement Timing


A liquidation intended to fund an obligation must have its proceeds become **available on
Opaca's derived settlement schedule (§5)** before the obligation becomes due — independent of
whether Alpaca paper trading credits cash immediately on fill.


Example (T+1 business-day calendar):


```text
Sale fill:       Sep 1
Derived settle:  Sep 2
Tax payment:     Sep 5


CHECK-12: PASS
```


---


## CHECK-13 — Runaway Agent Limit


Maximum autonomous orders per rolling hour.


Prevents a repeated trigger/repair loop from continuously trading.


---


## CHECK-14 — Minimum Trade Size


Reject dust trades.


---


## CHECK-15 — Pre-Close Blackout


Optional depending on broker spike result.


Avoid new autonomous trades during a configurable pre-close window if execution quality becomes unreliable.


---


## CHECK-16 — No Short Positions


Opaca is **long-only**.


* A projected post-trade position must never be negative.
* A sell quantity/notional may never exceed the reconciled long position available for
  liquidation.
* Broker capability to short (Phase −1A observed `shorting_enabled: true` on the paper
  account) must **never** be interpreted as policy permission.


---


# 10. Approval Model


Human approval does not bypass policy.


Flow:


```text
Proposal
↓
Policy validation
↓
Authority = ESCALATE
↓
Approval requested
↓
Human approves
↓
POLICY RUNS AGAIN
↓
Submit only if fresh PASS
```


Approval binds to:


* immutable proposal ID,
* hash of proposal payload,
* exact proposed action,
* expiry timestamp.


Recommended demo expiry:


**5 minutes**


If state changes between validation and execution:


```text
APPROVAL VOID
→ show delta
→ re-evaluate
```


Approver identity/authentication is intentionally **demo-grade**: a UI action by the
presenter is sufficient. No production authentication is required for the hackathon build.


---


# 11. Policy Repair Loop


A failed AI proposal is not automatically the end.


Flow:


```text
Agent proposal
↓
TreasuryGuard REJECT
↓
Return structured violations
↓
Agent gets ONE repair attempt
↓
Revalidate
```


If second attempt fails:


```text
NO_ACTION
```


Persist repair count.


Never create an unbounded self-correction loop.


## Scope of the repair loop (Amendment C)


The one-attempt repair loop applies **only** to a schema-valid proposal rejected by
TreasuryGuard policy.


LLM failures are **not** repairable and consume no repair attempt:


* timeout,
* unreachable,
* invalid JSON,
* schema violation (§7),
* unsupported decision value.


On any LLM failure:


```text
NO_ACTION
```


Execution remains blocked and the failure is audit-logged. Malformed output must **never**
be silently repaired into executable form.


---


# 12. Partial Fill Safety


Policy cannot assume all legs fill together.


For every multi-leg proposal evaluate dangerous subsets.


### Buy proposal


Check whether a single-leg fill could create excessive concentration.


### Sell proposal


Check liquidity assuming the liquidation does not fully fill.


After partial fill:


1. cancel remaining unresolved portion where appropriate,
2. reconcile realized positions,
3. recompute policy,
4. generate a fresh proposal.


Do not leave stale open orders drifting while the agent reasons from assumed state.


---


# 13. Order State Machine


Required states:


```text
PROPOSED
POLICY_VALIDATED
AUTO_AUTHORIZED
APPROVAL_REQUIRED
HUMAN_APPROVED
SUBMITTED
UNKNOWN
UNKNOWN_REQUIRES_REVIEW
ACCEPTED
NEW
PARTIALLY_FILLED
FILLED
REJECTED
EXPIRED
CANCELED
CANCELED_REMAINDER
RECONCILED
FAILED
```


## Critical UNKNOWN recovery (corrected)


If a network failure occurs after submission but before receipt of broker confirmation:


```text
DO NOT RESUBMIT
```


Recovery procedure:


1. query Alpaca by the deterministic `client_order_id`,
2. retry a **bounded** number of times with backoff,
3. if the order is located → resume normal status synchronization,
4. if still unresolved (including cases where the broker reports no such order) →
   transition to `UNKNOWN_REQUIRES_REVIEW`,
5. audit-log and surface the state in the UI,
6. **block automatic replacement and resubmission.**


Duplicate prevention takes priority over automatic recovery. Resolution of
`UNKNOWN_REQUIRES_REVIEW` requires operator action; the system must not auto-trade the
affected leg.


## Broker status mapping


An explicit **Alpaca-order-status → internal-state mapping table** must be committed
immediately after Phase −1 evidence is available (§17), covering all statuses Alpaca emits
(e.g. `new`, `accepted`, `partially_filled`, `filled`, `canceled`, `expired`, `rejected`,
`pending_new`, `done_for_day`, `held`, ...). Until that table exists, unmapped statuses must
fail closed into `UNKNOWN`.


---


# 14. Alpaca Architecture


One gateway:


```text
AlpacaGateway
```


Primary implementation:


**alpaca-py**


Responsibilities:


* account
* cash
* positions
* assets
* quotes
* market data
* submit orders
* query orders
* cancel orders
* reconciliation


## Explicit Non-Goals (Amendments E & F)


Opaca will NOT add:


* MCP order submission
* MCP order replacement/cancellation
* MCP reconciliation
* MCP write tools of any kind
* CLI invocation from runtime
* CLI as fallback broker integration
* a second broker source of truth
* a second account-state source of truth
* a second market-data provider
* a multi-agent supervisor/debate architecture
* a second LLM agent for risk
* live trading
* crypto
* another broker
* Opaca exposed as its own MCP server during this hackathon


## MCP Context Lane — OPTIONAL / POST-CORE (Amendment E)


> **Invariant: `MCP informs. alpaca-py adjudicates. TreasuryGuard enforces.`**


Opaca MAY use Alpaca MCP **only** for pre-proposal context gathering. It is governed by:


1. The lane is controlled by a feature flag:


   ```text
   MCP_CONTEXT_LANE=true|false
   ```


2. Default value: `false`.
3. Only **read-capable** MCP tools may be registered.
4. **Write-capable MCP tools MUST NOT be registered at all.** This includes any MCP tool
   that can submit orders, replace orders, cancel orders, or modify broker/account state.
5. MCP-derived data is **advisory input to the LLM proposal only.**
6. MCP-derived data MUST NOT:


   * become authoritative ledger state,
   * become reconciliation state,
   * determine execution authority,
   * bypass TreasuryGuard,
   * replace authoritative `alpaca-py` reads.
7. Before TreasuryGuard validates any proposal, authoritative broker state must be fetched
   again through the normal `alpaca-py` / Trading API path.
8. If MCP context disagrees with authoritative `alpaca-py` state: reject the proposal,
   log the discrepancy, require a fresh proposal.
9. If MCP times out, fails, is unavailable, or returns invalid/unusable data, Opaca must
   continue using the existing direct `alpaca-py` path. **MCP failure must never block
   core operation.**
10. If MCP is enabled, the complete MCP tool-call transcript is preserved as proposal
    provenance in the audit trail (`audit_events`, §15): tool name, sanitized arguments,
    timestamp, sanitized result, and correlation/proposal ID where available.
11. MCP implementation is **POST-CORE only.** Do not implement MCP until:


    * the core end-to-end Opaca flow is green,
    * broker execution and reconciliation are stable,
    * the hero liquidity-shock scenario works,
    * the demo can be repeated reliably.
12. MCP implementation is time-boxed to a maximum of **half a day**.
13. If MCP reduces reliability or threatens the submission timeline, remove/disable it.
14. MCP must never appear on:


    * the order submission path,
    * the order management path,
    * the reconciliation path,
    * the approval path,
    * the settlement calculation path.


## Alpaca CLI Operational Runbook (Amendment F)


> **Invariant: `CLI perturbs or inspects. Opaca detects. CLI never executes on behalf of Opaca.`**


1. The Alpaca CLI is **NOT** an Opaca runtime component.
2. Opaca code must **never** invoke the Alpaca CLI.
3. The CLI must not appear in: application imports, runtime subprocess calls, broker
   gateway logic, the policy engine, reconciliation logic, or the agent execution flow.
4. The CLI may be used manually for: paper-account inspection, operational diagnostics,
   pre-demo fixture setup while Opaca is stopped, and controlled out-of-band paper-account
   perturbation.
5. Recommended demo/testing use: use the Alpaca CLI manually to change paper-account state
   outside Opaca — e.g. place or alter a paper position externally, then restart/resume
   Opaca; Opaca reconciles against Alpaca, detects the unexpected broker-state drift, and
   surfaces the discrepancy.
6. The CLI acts as: a fault injector, an operator diagnostic tool, and a reconciliation
   test instrument.
7. It is NOT: a second execution interface, an Opaca dependency, a fallback order path, or
   part of production runtime architecture.
8. Reconciliation remains based on authoritative Alpaca API state, not CLI output.
9. If the CLI is unavailable, no core Opaca feature is impaired.


There must never be two conflicting sources of broker truth.


---


# 15. Database


SQLite.


Enable WAL.


Prefer a single serialized writer for state transitions.


Core tables:


```text
obligations          -- due_date stored as explicit ISO date (§4)
policies             -- seeded named defaults, see below
agent_proposals
proposal_legs
policy_checks
authority_decisions
approvals            -- includes payload_hash, expires_at
orders
reconciliations
audit_events
system_state
```


Important constraints:


```text
UNIQUE(proposal_id, leg_index)
UNIQUE(client_order_id)
```


## Seeded named policy defaults


Policy thresholds are explicit named rows seeded at initialization — never implicit
hard-coded magic numbers scattered through code. At minimum:


| Policy name                       | Default                          |
| --------------------------------- | -------------------------------- |
| concentration_max_pct             | 70%                              |
| per_order_autonomous_notional_max | set during build/rehearsal       |
| per_proposal_aggregate_notional_max | set during build/rehearsal     |
| rolling_24h_autonomous_notional_max | set during build/rehearsal     |
| rolling_autonomous_order_count_max | set during build/rehearsal      |
| runaway_hourly_order_count_max    | set during build/rehearsal       |
| min_trade_size                    | set during build/rehearsal       |
| preclose_blackout_window          | optional; set after Phase −1     |
| approval_expiry                   | 5 minutes                        |


Values live in the `policies` table and are displayed honestly in the UI.


---


# 16. Demo Reset


The reset mechanism is a first-class feature.


It must:


1. cancel appropriate outstanding paper orders,
2. restore all state Opaca controls (obligations, proposals, orders, approvals, policies, demo audit state),
3. reset scenario obligations — seeded as concrete ISO dates relative to reset time (§4),
4. reset policies,
5. clear/partition demo audit state,
6. **verify expected Alpaca cash and positions against the documented baseline before declaring success**,
7. fail visibly if reset is incomplete.


## Broker baseline reality (Amendment A)


Alpaca may not expose an API to configure paper cash or restore positions. Therefore:


* `demo_reset` restores everything Opaca controls, **then verifies broker state**;
* it must **never claim to reset broker cash** if Alpaca does not expose that capability.


If $500,000 cannot be reliably established:


* do **not** create a synthetic second cash balance;
* use actual Alpaca cash as the scenario base;
* deterministically scale seeded obligations/reserve amounts from that cash, preserving the
  intended economics and ratios;
* document the resulting demo values alongside the §4/§19 baselines;
* a documented **manual broker reset prerequisite** (e.g. dashboard reset before rehearsal)
  is acceptable if unavoidable — it is a pre-demo-day setup step, not part of in-demo flow.


Build this early.


We will rehearse the demo repeatedly.


---


# 17. Phase −1 — Broker Reality Spike


This is the **first coding work after the hackathon officially begins**.


Do not build the product until this passes.


Scope note: Phase −1 is based entirely on the Alpaca paper Trading API via `alpaca-py`.
MCP is out of scope for Phase −1 (§14, Amendment E); the Alpaca CLI is operator tooling
only and is never invoked by spike scripts (§14, Amendment F). Basic MCP/CLI availability
may optionally be noted later, but is never a gate.


## Questions to answer experimentally


### Account


* Can the paper balance be configured/reset to $500,000?
  * If yes: by what mechanism (API? dashboard only?), and is it reliably repeatable?
  * If no: what reset operations ARE available, and what baseline can be documented instead (§3, §16)?
* What values are returned for:
  * cash
  * buying_power
  * non_marginable_buying_power
  * multiplier?


### Assets


Confirm:


* SGOV tradable
* BIL tradable
* SHV tradable


### Orders


Test:


* whole-share market
* whole-share limit
* fractional quantity
* notional order
* extended-hours eligibility where relevant


### Client order IDs


* Determine the actual Alpaca `client_order_id` constraints (charset, maximum length, uniqueness scope).
* Confirm our deterministic `hash(proposal_id + leg_index)` encoding satisfies them (CHECK-09, §9).


### Lifecycle


Capture actual transitions for the full status set Alpaca emits — including any beyond
`accepted/new/partially_filled/filled/canceled/rejected/expired` (e.g. `pending_new`,
`done_for_day`) — as the evidence base for the §13 status-mapping table.


### Idempotency


Submit identical `client_order_id`.


Record exact Alpaca behaviour.


### Crash Recovery


1. submit,
2. interrupt process/network,
3. restart,
4. recover by client order ID (bounded retries with backoff),
5. prove no duplicate order,
6. document behaviour when the order cannot be located at all (feeds §13 `UNKNOWN_REQUIRES_REVIEW`).


### Settlement


Sell an instrument.


Observe:


* account cash,
* available/transferable cash where exposed,
* actual paper settlement/crediting behaviour,
* reconciliation timing.


Note: this records actual paper behaviour only. Opaca's settlement model is derived (§5,
Amendment B) and must remain correct even if paper credits proceeds instantly.


### Calendar / Clock


* Confirm access to trading clock and trading-calendar endpoints needed for the T+1 US
  securities business-day calendar (weekend/holiday rolls) and CHECK-15 blackout window.


## Gate


Do not proceed until actual behaviour is documented in:


```text
docs/broker-reality-spike.md
```


with raw evidence where useful.


---


# 18. Revised Build Order


## Phase −1


Broker Reality Spike


## Phase 0


Repo + minimal backend/frontend shell + demo reset


## Phase 1


Cash/liability engine + settlement model


## Phase 2


TreasuryGuard policy engine


## Phase 3


AlpacaGateway read path


## Phase 4


Agent maturity-matching proposal


## Phase 5


Execution + idempotency + state machine


## Phase 6


Reconciliation


## Phase 7


Thin end-to-end UI


## Phase 8


Liquidity shock hero flow


Target: thin hero flow working by **Day 4**.


## Phase 9


Firewall demonstration + failure states


## Phase 10


Visual polish


## Phase 11


README / architecture / pitch / submission


## Phase 12


Video recording


**Hard stop: record the video no later than Day 6.**


Day 7 is backup/submission/QC.


MCP (§14, Amendment E) is deliberately **not** a build-order phase. It may be attempted only
after the hero flow and demo repeatability are green (i.e. after Phase 9), time-boxed to
half a day, and removed if it threatens reliability or the submission timeline.


---


# 19. Demo Narrative


Values below assume the documented $500,000 baseline (§3, §16). If the scaled-baseline
fallback is active, substitute the documented scaled values throughout.


## Beat 1 — Corporate Cash


```text
Cash             $500,000
Protected        $390,000
Investable       $110,000
```


---


## Beat 2 — Agent Decision


Opaca analyzes obligation horizons.


It produces a maturity-aware allocation.


Routine transaction is inside policy and authority.


```text
AUTO
```


No human click.


---


## Beat 3 — Firewall Proof


Show one intentionally invalid proposal.


Example:


```text
Projected SGOV concentration = 84%
Policy maximum = 70%


CHECK-04: FAIL
EXECUTION BLOCKED
```


This proves TreasuryGuard is real.


---


## Beat 4 — Genuine Alpaca Execution


Actual paper order.


Show:


* Alpaca order ID,
* status,
* fill,
* reconciled position.


No synthetic fills.


---


## Beat 5 — Simulated External Event


A mocked ERP/webhook event arrives:


```text
NEW TAX LIABILITY
$100,000
DUE IN 7 DAYS
```


The presenter does not manually type it into a form.


---


## Beat 6 — Opaca Reacts


Projected future liquidity breaches policy.


Opaca proposes liquidation.


---


## Beat 7 — Governance


TreasuryGuard:


```text
POLICY: PASS
AUTHORITY: APPROVAL_REQUIRED
```


Reason:


* exceptional liquidity event,
* action exceeds autonomous authority.


CFO approves.


Policy immediately re-runs.


---


## Beat 8 — Execute + Settlement


Alpaca sell executes.


Opaca reconciles actual fill.


It computes settlement from its derived T+1 schedule (§5):


Example:


```text
Sell settles: Sep 2
Tax due:      Sep 5


CHECK-12: PASS
```


---


## Beat 9 — Climax


```text
30-DAY LIQUIDITY HEADROOM


BEFORE EVENT: +$10,000
AFTER EVENT:  -$90,000
AFTER ACTION: +$10,000


LIQUIDITY RESTORED ✓
```


The climax is not that Alpaca can execute an order.


The climax is:


> **An autonomous agent discovered a liquidity problem, proposed a response, was constrained by governance, escalated correctly, executed through Alpaca, and proved the cash would settle before the company needed it.**


---


# 20. Failure Gates


The following must be demonstrably safe:


### Kill during submission


Restart produces **zero duplicate orders**.


### Lose network during reconciliation


UI shows:


```text
RECONCILIATION PENDING
```


No new trade is submitted.


### Order cannot be confirmed after submission


Bounded retry by `client_order_id`, then `UNKNOWN_REQUIRES_REVIEW`. Surfaced, audited,
and **no automatic resubmission or replacement** (§13).


### LLM unavailable or malformed (Amendment C)


Any of: timeout, unreachable, invalid JSON, schema violation, unsupported decision →


```text
NO_ACTION
```


Execution blocked, failure audit-logged, malformed output never repaired into executable form.


### Agent proposes policy violation


TreasuryGuard blocks it.


### Agent fails second repair attempt


System ends:


```text
NO_ACTION
```


### Market/broker unavailable


System fails closed.


### Approval expires


Order does not execute.


### Proposal mutates after approval


Approval becomes invalid.


---


# 21. Definition of Done


Opaca is not complete until all are true:


* [ ] Broker Reality Spike documented.
* [ ] Single cash ledger enforced.
* [ ] No leverage path possible.
* [ ] Cash and obligations deterministic; obligations stored as explicit ISO dates.
* [ ] Settlement-aware liquidity implemented via the derived T+1 business-day schedule, correct regardless of Alpaca paper crediting behaviour.
* [ ] Agent performs maturity/horizon reasoning.
* [ ] Agent can choose HOLD (`allocations` empty).
* [ ] Strict LLM schema validation implemented; unknown fields rejected.
* [ ] LLM timeout/unreachable/invalid output fails closed to NO_ACTION with audit trail.
* [ ] target_weight validated per decision type (allocate / liquidate / rebalance / hold) with schema invariants and tests.
* [ ] TreasuryGuard policy engine complete.
* [ ] Kill switch works.
* [ ] Aggregate autonomy limits work.
* [ ] Existing positions included in concentration.
* [ ] Partial-fill-safe policy implemented.
* [ ] Approval hash + expiry implemented.
* [ ] Revalidation occurs before every approved order.
* [ ] Deterministic client order IDs implemented and verified against actual Alpaca ID constraints.
* [ ] DB uniqueness enforces idempotency.
* [ ] UNKNOWN recovery works: bounded retries, then UNKNOWN_REQUIRES_REVIEW; zero automatic resubmissions.
* [ ] Alpaca-status → internal-state mapping table documented from Phase −1 evidence.
* [ ] Alpaca paper trade executes.
* [ ] Actual broker positions reconcile.
* [ ] Invalid proposal visibly blocked.
* [ ] Baseline routine trade auto-executes.
* [ ] Liquidity shock is automatically detected.
* [ ] Exceptional response escalates.
* [ ] Sell settlement precedes obligation date per the derived schedule.
* [ ] Liquidity restoration is mathematically defensible.
* [ ] Audit trail captures complete lifecycle, including LLM failures.
* [ ] Named policy defaults seeded in the `policies` table.
* [ ] No MCP write tools registered, MCP context lane disabled by default, and no runtime Alpaca CLI invocation anywhere in the codebase (Amendments E, F).
* [ ] Long-only enforced (CHECK-16): no sell may exceed the reconciled long position; no projected post-trade position may be negative.
* [ ] Demo can be restored reliably to a **documented known broker state**, and Opaca **verifies that state** before proceeding (Amendment A).
* [ ] Full demo can be repeated without developer intervention (any manual broker reset prerequisite is a documented one-time pre-demo step).
* [ ] Submission video recorded by Day 6.


---


# 22. Frozen Positioning


## Name


**Opaca**


## Subtitle


**Autonomous Corporate Cash Agent powered by Alpaca**


## One-liner


> **Most trading agents ask what to buy. Opaca first asks what money the business can safely afford to invest.**


## Intelligence


> **Liquidity-aware maturity matching**


## Architecture


> **AI reasons. Software enforces. Alpaca executes.**


## Autonomy


> **Autonomous when policy allows. Human when judgment matters.**


## Finance-grade claim


> **No leverage. No stale approvals. No duplicate trades. No unsettled proceeds pretending to be liquidity.**


---


# 23. Freeze Rule


No new feature enters the MVP unless it:


1. fixes a correctness problem,
2. materially strengthens one judging criterion,
3. materially improves the hero demo,
4. or is required by final hackathon rules.


Otherwise:


**CUT IT.**
