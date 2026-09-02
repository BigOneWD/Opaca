# Wheel Competition Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the smallest defensible Competition Wheel Mode that can take an AI/MCP-derived CSP intent, deterministically select and risk-check a real Alpaca option, reserve assignment capital atomically, execute one PAPER option order through `alpaca-py`, reconcile the exact short-put position, and emit sanitized proof.

**Architecture:** Keep the verified treasury/equity path frozen. Add a focused `opaca.wheel` package with separate option models, persistence, policy/authority, broker read/write adapters, reconciliation, and agent/MCP boundary. Alpaca MCP is observation-only; authoritative broker truth and mutation remain in `alpaca-py`.

**Tech Stack:** Python >=3.11, pytest, SQLite/WAL, `alpaca-py==0.33.0`, existing Opaca domain/policy/authority primitives, stdlib JSON/hashlib/datetime/decimal.

**Spec:** `docs/superpowers/specs/2026-09-02-wheel-competition-mode-design.md` plus `docs/superpowers/specs/2026-09-02-wheel-competition-mode-final-review-amendment.md`. If they conflict, the final-review amendment wins.

## Global Constraints

- PAPER only. No live-money code path.
- Strict TDD: no production code before the intended failing test is observed.
- No broker mutation in unit/integration tests. Real PAPER mutation is a distinct final proof step.
- `risk_capital_base` is immutable opening competition cash; current `reconciled_cash` is Alpaca `account.cash` from a successful Wheel reconciliation snapshot.
- `buying_power`, `options_buying_power`, and equity are diagnostics only and never enlarge Opaca authority.
- One new opening CSP contract per V1 decision.
- PUT only, 1–7 DTE, DAY limit orders, no new opening in final 30 minutes of regular session.
- Option quote age <=15 seconds; future timestamps fail closed.
- Premium yield on assignment >=0.001.
- Hard per-underlying Wheel exposure <=25% of `risk_capital_base`.
- AUTO only when proposal <=10%, post-trade underlying <=10%, and post-trade aggregate Wheel exposure <=20% of `risk_capital_base`.
- Hard REJECT is never human-overridable.
- Approval TTL = 5 minutes; approval is bound to exact run/attempt/contract/action/quantity/assignment capital/limit and must be fully revalidated before submission.
- One AI repair only: `MAX_AGENT_ATTEMPTS_PER_RUN = 2`.
- MCP tool surface is exact allowlist and must fail closed before model invocation if any non-allowlisted tool is exposed.
- Existing treasury/equity DB and execution service remain unchanged.

---

## Planned File Structure

New package:

- `backend/opaca/wheel/__init__.py` — public Wheel types/functions only.
- `backend/opaca/wheel/models.py` — typed option, intent, position, exposure, policy and approval records.
- `backend/opaca/wheel/config.py` — immutable V1 constants/policy values.
- `backend/opaca/wheel/adapters.py` — fail-closed Alpaca option/account payload adaptation.
- `backend/opaca/wheel/read_gateway.py` — read-only authoritative `alpaca-py` protocol/adapter for account, options, positions, orders and clock.
- `backend/opaca/wheel/store.py` — fresh Wheel SQLite schema, account binding, reservations, share lots, orders, approvals, audits.
- `backend/opaca/wheel/exposure.py` — held-share, reservation, per-name and aggregate exposure arithmetic.
- `backend/opaca/wheel/selector.py` — deterministic CSP selection from validated intent + authoritative contracts/quote.
- `backend/opaca/wheel/policy.py` — CHECK-17..22 hard policy evaluation.
- `backend/opaca/wheel/authority.py` — Wheel delegated AUTO/APPROVAL logic plus existing rolling/runaway constraints.
- `backend/opaca/wheel/order_id.py` — deterministic option logical-order identity.
- `backend/opaca/wheel/execution.py` — slim PAPER option mutation boundary.
- `backend/opaca/wheel/reconciliation.py` — post-submit and expiry/assignment reconciliation.
- `backend/opaca/wheel/mcp_guard.py` — exact MCP tool allowlist enforcement.
- `backend/opaca/wheel/agent.py` — bounded OptionIntent + repair orchestration with no broker mutation capability.
- `backend/opaca/wheel/evidence.py` — sanitized JSON evidence artifact.
- `backend/tests/wheel/` — focused test suite mirroring package responsibilities.

Do not refactor the existing equity modules unless a task below explicitly names them.

---

### Task 1: Read-Only Feasibility Probe — No Production Code

**Files:**
- Create: `docs/evidence/wheel-readonly-feasibility-2026-09-02.md`
- No production/test code changes.

**Interfaces:**
- Consumes: current PAPER credentials loaded without printing values.
- Produces: observed field/tool names used by Tasks 3, 6, 10, 11.

- [ ] **Step 1: Verify repository and branch**

Run:

```bash
cd /Users/macmini/Projects/Opaca
git status --short --branch
git fetch origin
git switch feat/wheel-competition-mode
git pull --ff-only origin feat/wheel-competition-mode
git rev-parse HEAD
```

Expected: clean worktree and HEAD at or after `8b8c7aa01d81a7938fc32886668b4c3e9d2ed401`.

- [ ] **Step 2: Load PAPER credentials without echoing them**

```bash
unset APCA_API_KEY_ID APCA_API_SECRET_KEY APCA_API_BASE_URL
set -a
source /Users/macmini/Projects/Opaca/.env
set +a
python - <<'PY'
import os
assert os.environ.get('APCA_API_KEY_ID')
assert os.environ.get('APCA_API_SECRET_KEY')
assert os.environ.get('APCA_API_BASE_URL') == 'https://paper-api.alpaca.markets'
print('PAPER env: OK')
PY
```

- [ ] **Step 3: Run a read-only `alpaca-py` probe**

Use installed `alpaca-py==0.33.0` to print only sanitized names/shape, never credential values or full account id. Record:

```text
account.cash
account.options_buying_power field name/presence
account.options_approved_level
account.options_trading_level
account status/trading_blocked
one liquid underlying price
one PUT contract payload: symbol, underlying, strike, expiry, multiplier, tradable/status
one option quote payload: bid, ask, source timestamp fields
current market clock
```

Do not submit/cancel/replace/exercise anything.

- [ ] **Step 4: Probe Alpaca MCP read-only surface**

If the MCP server is already configured, list exposed tool names without invoking mutation tools. If not configured, inspect the official installed/configured server command and configure it read-only; do not enable trading/account mutation toolsets. Record the exact exposed read tool names and verify that mutation names such as `place_option_order`, exercise, cancel/replace and account mutation are absent.

- [ ] **Step 5: Commit evidence only**

```bash
git add docs/evidence/wheel-readonly-feasibility-2026-09-02.md
git commit -m "docs: capture wheel read-only feasibility"
```

**Gate:** stop here and report if account/option contract/multiplier/quote timestamp/MCP tool surface cannot be observed safely. No production code before this evidence exists.

---

### Task 2: Wheel Domain Models and V1 Configuration

**Files:**
- Create: `backend/opaca/wheel/__init__.py`
- Create: `backend/opaca/wheel/models.py`
- Create: `backend/opaca/wheel/config.py`
- Test: `backend/tests/wheel/test_models.py`

**Interfaces:**
- Produces: `WheelAction`, `OptionRight`, `WheelState`, `OptionContract`, `OptionQuote`, `OptionIntent`, `OptionPosition`, `WheelShareLot`, `WheelPolicy`, `WheelApprovalBinding`.

- [ ] **Step 1: Write failing model validation tests**

Cover at minimum:

```python
assert OptionRight.PUT.value == "PUT"
assert WheelState.CASH.value == "CASH"
with pytest.raises(ValueError):
    OptionContract(..., multiplier=Decimal("0"))
with pytest.raises(ValueError):
    OptionQuote(bid=Decimal("-1"), ask=Decimal("1"), as_of=NOW)
with pytest.raises(ValueError):
    OptionIntent(action=WheelAction.SELL_CASH_SECURED_PUT, underlying="", ...)
```

`WheelPolicy` defaults must equal the amendment constants: 1–7 DTE, 15s quote age, 30m preclose blackout, 0.001 min premium yield, 0.25 hard per-name, 0.10 AUTO proposal/per-name, 0.20 AUTO aggregate, 5m approval TTL, one contract.

- [ ] **Step 2: Run RED**

```bash
cd /Users/macmini/Projects/Opaca/backend
pytest tests/wheel/test_models.py -q
```

Expected: import/module failure because `opaca.wheel` does not exist.

- [ ] **Step 3: Implement minimal frozen enums/dataclasses and policy constants**

Use `Decimal`, timezone-aware UTC datetimes, positive multiplier, non-negative quotes, positive strike, integer contract counts. Do not add Greeks or multi-leg concepts.

- [ ] **Step 4: Run GREEN + type/lint slice**

```bash
pytest tests/wheel/test_models.py -q
ruff check opaca/wheel/models.py opaca/wheel/config.py tests/wheel/test_models.py
mypy opaca/wheel/models.py opaca/wheel/config.py tests/wheel/test_models.py
```

- [ ] **Step 5: Commit**

```bash
git add backend/opaca/wheel backend/tests/wheel/test_models.py
git commit -m "feat: add wheel option domain models"
```

---

### Task 3: Fresh Wheel Store, Account Binding, and Immutable Risk Capital

**Files:**
- Create: `backend/opaca/wheel/store.py`
- Test: `backend/tests/wheel/test_store.py`

**Interfaces:**
- Produces: `WheelStore`, `WheelAccountBinding`, `WheelReservation`, `WheelOrderRecord`, `WheelAuditEvent`.
- Methods used later: `bootstrap_account(account_id: str, opening_cash: Decimal, now: datetime)`, `assert_account_binding(account_id: str)`, `risk_capital_base()`, `active_assignment_reservations()`, `begin_immediate()`.

- [ ] **Step 1: Write RED tests**

Tests must prove:

```text
fresh DB uses WAL + foreign_keys
bootstrap stores SHA-256(account_id), never raw account id
risk_capital_base seeds once from opening cash
second bootstrap with same account cannot rescale capital
second bootstrap with different account fails closed
premium/current-cash changes cannot alter risk_capital_base
```

- [ ] **Step 2: Run RED**

```bash
pytest tests/wheel/test_store.py -q
```

- [ ] **Step 3: Implement a dedicated Wheel schema**

Tables must be limited to V1 needs: `wheel_meta`, `wheel_reservations`, `wheel_share_lots`, `wheel_orders`, `wheel_approvals`, `wheel_audit`. Use `BEGIN IMMEDIATE`, WAL and foreign keys. Persist decimal values as canonical strings, following existing persistence codec patterns.

- [ ] **Step 4: Run GREEN and regression**

```bash
pytest tests/wheel/test_store.py tests/test_atomic_reservation.py -q
ruff check opaca/wheel/store.py tests/wheel/test_store.py
mypy opaca/wheel/store.py tests/wheel/test_store.py
```

- [ ] **Step 5: Commit**

```bash
git add backend/opaca/wheel/store.py backend/tests/wheel/test_store.py
git commit -m "feat: add isolated wheel persistence"
```

---

### Task 4: Option-Safe Broker Adapters and Authoritative Read Protocol

**Files:**
- Create: `backend/opaca/wheel/adapters.py`
- Create: `backend/opaca/wheel/read_gateway.py`
- Test: `backend/tests/wheel/test_adapters.py`
- Test: `backend/tests/wheel/test_read_gateway.py`

**Interfaces:**
- Produces: `adapt_option_contract`, `adapt_option_quote`, `adapt_option_position`, `OptionReadGateway` protocol, `AlpacaOptionReadGateway`.

- [ ] **Step 1: RED adapter tests from recorded sanitized Task-1 payload shapes**

Prove fail-closed behavior for missing/zero multiplier, malformed strike, non-PUT right where PUT is required, naive/future-invalid quote timestamp inputs, and a short option position that must be accepted without weakening existing equity `adapt_position()`.

- [ ] **Step 2: Run RED**

```bash
pytest tests/wheel/test_adapters.py tests/wheel/test_read_gateway.py -q
```

- [ ] **Step 3: Implement read-only gateway**

The protocol must expose only the reads needed by V1: account, option contracts, option quote, option positions, equity positions, relevant open/order-by-client-id read, and market clock. The concrete adapter uses the exact `alpaca-py==0.33.0` APIs observed in Task 1. It contains no submit/cancel/replace/exercise methods.

- [ ] **Step 4: Run GREEN and verify old adapter unchanged**

```bash
pytest tests/wheel/test_adapters.py tests/wheel/test_read_gateway.py tests/test_broker_adapters.py -q
```

- [ ] **Step 5: Commit**

```bash
git add backend/opaca/wheel/adapters.py backend/opaca/wheel/read_gateway.py backend/tests/wheel
git commit -m "feat: add option-safe broker reads"
```

---

### Task 5: Exposure Accounting and Assignment Reservations

**Files:**
- Create: `backend/opaca/wheel/exposure.py`
- Modify: `backend/opaca/wheel/store.py`
- Test: `backend/tests/wheel/test_exposure.py`
- Test: `backend/tests/wheel/test_reservations.py`

**Interfaces:**
- Produces: `compute_wheel_exposure(...) -> WheelExposure`, `reserve_assignment(...)`, `release_assignment_if_proven_no_exposure(...)`, `convert_assignment_to_share_lot(...)`.

- [ ] **Step 1: RED arithmetic tests**

Must prove:

```text
available_cash = reconciled_cash - ACTIVE assignment reservations
held_share_exposure = max(Wheel cost basis, reconciled market value)
per-name exposure = held share exposure + active reservation on that name
aggregate exposure = all held-share exposure + all active reservations
assignment transition creates share lot before releasing reservation
manual/unattributable share mismatch -> UNKNOWN/fail-closed result
```

Include the Opus bypass case: assign a 25k CSP, release reservation, then prove a new same-name CSP is rejected because assigned shares still consume concentration capacity.

- [ ] **Step 2: Run RED**

```bash
pytest tests/wheel/test_exposure.py tests/wheel/test_reservations.py -q
```

- [ ] **Step 3: Implement minimal exposure functions/store operations**

No P&L optimizer. Preserve reservation on UNKNOWN. Release only on broker-proven zero-fill/no-position terminal outcome, expiry-worthless proof, or assignment conversion in one transaction.

- [ ] **Step 4: Run GREEN**

```bash
pytest tests/wheel/test_exposure.py tests/wheel/test_reservations.py tests/test_execution_reservations.py -q
```

- [ ] **Step 5: Commit**

```bash
git add backend/opaca/wheel/exposure.py backend/opaca/wheel/store.py backend/tests/wheel
git commit -m "feat: enforce wheel exposure accounting"
```

---

### Task 6: Deterministic CSP Selector and Hard Wheel Policy

**Files:**
- Create: `backend/opaca/wheel/selector.py`
- Create: `backend/opaca/wheel/policy.py`
- Modify: `backend/opaca/domain/models.py` — append `CHECK_17` through `CHECK_22` only.
- Test: `backend/tests/wheel/test_selector.py`
- Test: `backend/tests/wheel/test_policy.py`

**Interfaces:**
- Produces: `select_csp(intent, contracts, quote_by_symbol, policy, now, session_close) -> SelectedCsp`, `WheelGuardEngine.evaluate(context, proposal) -> PolicyDecision`.

- [ ] **Step 1: RED selector tests**

Prove deterministic ordering: permitted PUT, 1–7 DTE, not within final 30m, strike closest to ownership ceiling without exceeding, earliest expiry, lexical OCC tie-break, one contract, DAY limit at bid. Selector must not move to a safer strike to pass exposure policy.

- [ ] **Step 2: RED hard-policy tests**

Map exact checks:

```text
CHECK-17 WHEEL_STATE
CHECK-18 OPTION_CONTRACT
CHECK-19 OPTION_QUOTE_ECONOMICS
CHECK-20 WHEEL_EXPOSURE_CASH
CHECK-21 BROKER_OPTION_COLLATERAL
CHECK-22 COMPETITION_ACCOUNT_BINDING
```

Tests must include 15s freshness, future timestamp reject, minimum yield 0.001, per-name 25% hard cap using held shares + reservations + proposed, aggregate <= risk capital, available cash, collateral contradiction, kill switch/environment/idempotency inherited checks where applicable.

- [ ] **Step 3: Run RED**

```bash
pytest tests/wheel/test_selector.py tests/wheel/test_policy.py -q
```

- [ ] **Step 4: Implement minimal selector/guard and new CheckIds**

Keep existing `TreasuryGuardEngine` untouched. Reuse `PolicyCheckResult`/`PolicyDecision` vocabulary.

- [ ] **Step 5: Run GREEN + old policy regression**

```bash
pytest tests/wheel/test_selector.py tests/wheel/test_policy.py tests/test_policy.py -q
ruff check opaca/wheel/selector.py opaca/wheel/policy.py tests/wheel
mypy opaca/wheel/selector.py opaca/wheel/policy.py tests/wheel/test_selector.py tests/wheel/test_policy.py
```

- [ ] **Step 6: Commit**

```bash
git add backend/opaca/wheel backend/opaca/domain/models.py backend/tests/wheel
git commit -m "feat: add deterministic csp risk policy"
```

---

### Task 7: Delegated Wheel Authority, Approval Binding, and Option Order Identity

**Files:**
- Create: `backend/opaca/wheel/authority.py`
- Create: `backend/opaca/wheel/order_id.py`
- Modify: `backend/opaca/wheel/store.py`
- Test: `backend/tests/wheel/test_authority.py`
- Test: `backend/tests/wheel/test_order_id.py`
- Test: `backend/tests/wheel/test_approval.py`

**Interfaces:**
- Produces: `decide_wheel_authority(...) -> AuthorityDecision`, `wheel_client_order_id(...) -> str`, approval persist/load/expiry validation.

- [ ] **Step 1: RED authority tests**

Prove:

```text
8% proposal / 8% post-name / 8% aggregate -> AUTO
8% proposal / 8% post-name / 21% aggregate -> APPROVAL_REQUIRED
12% proposal within hard 25% -> APPROVAL_REQUIRED
hard policy failure -> REJECT
existing stricter rolling/runaway limit can only tighten authority
```

- [ ] **Step 2: RED identity/approval tests**

Same run+attempt+OCC+action+contracts+limit => same client ID. Any changed bound field => different ID. Approval expires exactly at 5 minutes and binds all amendment fields.

- [ ] **Step 3: Run RED**

```bash
pytest tests/wheel/test_authority.py tests/wheel/test_order_id.py tests/wheel/test_approval.py -q
```

- [ ] **Step 4: Implement minimum code**

Reuse `AuthorityResult` and existing rolling/runaway helpers rather than copying their algorithms.

- [ ] **Step 5: Run GREEN + existing authority regression**

```bash
pytest tests/wheel/test_authority.py tests/wheel/test_order_id.py tests/wheel/test_approval.py tests/test_authority.py -q
```

- [ ] **Step 6: Commit**

```bash
git add backend/opaca/wheel backend/tests/wheel
git commit -m "feat: add wheel delegated authority"
```

---

### Task 8: Atomic Final Recheck and Option Order Lifecycle — Still No Real Broker Mutation

**Files:**
- Create: `backend/opaca/wheel/lifecycle.py`
- Modify: `backend/opaca/wheel/store.py`
- Test: `backend/tests/wheel/test_atomic_reservation.py`
- Test: `backend/tests/wheel/test_lifecycle.py`

**Interfaces:**
- Produces: `authorize_and_reserve(...) -> AuthorizedWheelOrder`, lifecycle state mapping for submitted/open/filled/cancelled/rejected/unknown.

- [ ] **Step 1: RED concurrency tests**

Using two SQLite writer connections, prove two callers cannot both consume the same unreserved cash. Inside one `BEGIN IMMEDIATE`, assert snapshot version/account fingerprint, recompute reservation-dependent exposure/available cash, create one reservation, persist logical order.

- [ ] **Step 2: RED release tests**

`UNKNOWN`, timeout, cancel-pending retain reservation. Broker-confirmed rejected/cancelled/day-expired/NOT_SUBMITTED with zero fill + exact OCC absence + no unresolved same client id releases it. FILLED keeps it.

- [ ] **Step 3: Run RED**

```bash
pytest tests/wheel/test_atomic_reservation.py tests/wheel/test_lifecycle.py -q
```

- [ ] **Step 4: Implement minimal transaction/lifecycle**

Do not call Alpaca mutation APIs in this task.

- [ ] **Step 5: Run GREEN + existing atomic regression**

```bash
pytest tests/wheel/test_atomic_reservation.py tests/wheel/test_lifecycle.py tests/test_atomic_reservation.py -q
```

- [ ] **Step 6: Commit**

```bash
git add backend/opaca/wheel backend/tests/wheel
git commit -m "feat: add atomic wheel reservation lifecycle"
```

---

### Task 9: Post-Fill Option Reconciliation

**Files:**
- Create: `backend/opaca/wheel/reconciliation.py`
- Modify: `backend/opaca/wheel/store.py`
- Test: `backend/tests/wheel/test_reconciliation.py`

**Interfaces:**
- Produces: `reconcile_wheel(...) -> WheelReconciliationResult` and persisted per-underlying `WheelState`.

- [ ] **Step 1: RED post-fill tests**

`RECONCILED + SHORT_PUT_OPEN` requires exact local authorized order, broker read-back same client id/OCC, matching short PUT qty, ACTIVE assignment reservation equal to assignment capital, same account fingerprint. Missing either order/position/reservation => UNKNOWN, not success.

- [ ] **Step 2: RED assignment/expiry tests**

Encode amendment constants exactly:

```text
expiry proof only after 09:35 America/New_York on next regular trading session
assignment cash tolerance = max($5, 0.0005 × assignment capital)
```

Assignment must create/update share lot before reservation release. Contradiction/manual shares/ambiguous delta => UNKNOWN.

- [ ] **Step 3: Run RED**

```bash
pytest tests/wheel/test_reconciliation.py -q
```

- [ ] **Step 4: Implement minimal reconciliation**

No periodic scheduler. Expose deterministic function/CLI callable triggers for startup, pre-run, post-order change and explicit reconciliation.

- [ ] **Step 5: Run GREEN + existing reconciliation regression**

```bash
pytest tests/wheel/test_reconciliation.py tests/test_reconciliation.py -q
```

- [ ] **Step 6: Commit**

```bash
git add backend/opaca/wheel/reconciliation.py backend/opaca/wheel/store.py backend/tests/wheel/test_reconciliation.py
git commit -m "feat: reconcile short put wheel state"
```

---

### Task 10: Slim `alpaca-py` PAPER Option Execution Boundary

**Files:**
- Create: `backend/opaca/wheel/execution.py`
- Test: `backend/tests/wheel/test_execution.py`
- Test: `backend/tests/wheel/test_execution_mutation_boundary.py`

**Interfaces:**
- Produces: `OptionExecutionGateway` protocol, concrete PAPER gateway, `submit_authorized_csp(...)`.

- [ ] **Step 1: RED tests with fake gateway only**

Prove submit cannot occur unless: PAPER endpoint verified, kill switch clear, account fingerprint current, reservation exists, approval valid when required, fresh <=15s authoritative quote, current bid >= persisted sell limit, final policy still passes. Unknown exception after submit attempt records UNKNOWN and leaves reservation ACTIVE; no automatic retry.

- [ ] **Step 2: Run RED**

```bash
pytest tests/wheel/test_execution.py tests/wheel/test_execution_mutation_boundary.py -q
```

- [ ] **Step 3: Implement minimal concrete adapter**

Use exact `alpaca-py==0.33.0` option-order request API observed locally. V1 only: single-leg SELL limit DAY opening PUT. Do not expose cancel-all, close-all, exercise, live endpoint, spread or multi-leg helpers.

- [ ] **Step 4: Run GREEN and source safety scan**

```bash
pytest tests/wheel/test_execution.py tests/wheel/test_execution_mutation_boundary.py -q
rg -n "LIVE|close_all|cancel_all|exercise|multi.?leg" opaca/wheel
```

Review each match; only explicit guards/comments/tests are acceptable.

- [ ] **Step 5: Commit**

```bash
git add backend/opaca/wheel/execution.py backend/tests/wheel
git commit -m "feat: add paper-only csp execution"
```

---

### Task 11: Enforced MCP Read-Only Tool Surface and Bounded Agent Repair

**Files:**
- Create: `backend/opaca/wheel/mcp_guard.py`
- Create: `backend/opaca/wheel/agent.py`
- Test: `backend/tests/wheel/test_mcp_guard.py`
- Test: `backend/tests/wheel/test_agent.py`

**Interfaces:**
- Produces: `assert_mcp_tool_surface(exposed, allowed, required)`, `WheelIntentProvider` protocol, `run_wheel_decision(...)`.

- [ ] **Step 1: RED MCP tests**

Populate `MCP_ALLOWED_TOOLS` from Task-1 evidence, not guessed documentation. Inject `place_option_order` and prove failure occurs before `WheelIntentProvider` is invoked. Also fail if a required read tool is missing.

- [ ] **Step 2: RED repair-loop tests**

Run id has exactly attempts 1 and 2. First REJECT feedback contains only check codes/authoritative arithmetic. Second REJECT => `NO_COMPLIANT_TRADE` and no execution call. AI may output only `OptionIntent`; OCC/quantity/multiplier/limit/policy are rejected if present in provider payload schema.

- [ ] **Step 3: Run RED**

```bash
pytest tests/wheel/test_mcp_guard.py tests/wheel/test_agent.py -q
```

- [ ] **Step 4: Implement minimal runner**

Keep provider interface injectable so the demo can use the available AI/MCP client without coupling broker mutation to the model process.

- [ ] **Step 5: Run GREEN**

```bash
pytest tests/wheel/test_mcp_guard.py tests/wheel/test_agent.py -q
```

- [ ] **Step 6: Commit**

```bash
git add backend/opaca/wheel/mcp_guard.py backend/opaca/wheel/agent.py backend/tests/wheel
git commit -m "feat: add bounded read-only wheel agent"
```

---

### Task 12: Sanitized Evidence, CLI Wiring, Full Dry Workflow

**Files:**
- Create: `backend/opaca/wheel/evidence.py`
- Create: `backend/opaca/wheel/cli.py`
- Modify: `backend/opaca/__main__.py`
- Test: `backend/tests/wheel/test_evidence.py`
- Test: `backend/tests/wheel/test_cli.py`

**Interfaces:**
- Produces CLI commands `wheel-probe`, `wheel-plan`, `wheel-reconcile`, and a separately opt-in `wheel-submit-paper` mutation command.

- [ ] **Step 1: RED evidence/CLI tests**

Sanitized artifact must contain mode, PAPER, underlying, OCC, action, one contract, strike, multiplier, assignment capital, authority, broker/client IDs, broker status, Wheel state, reserved capital, reconciliation. Assert no API key, secret, token, raw account id/account number.

- [ ] **Step 2: Run RED**

```bash
pytest tests/wheel/test_evidence.py tests/wheel/test_cli.py -q
```

- [ ] **Step 3: Implement CLI with mutation opt-in separation**

Default commands are read-only. `wheel-submit-paper` must require an explicit mutation opt-in flag/environment guard consistent with existing PAPER mutation test conventions.

- [ ] **Step 4: Run full local suite and quality gates**

```bash
pytest -q
ruff check opaca tests
mypy opaca tests
git diff --check
```

All must pass before any real options mutation.

- [ ] **Step 5: Commit**

```bash
git add backend/opaca backend/tests/wheel
git commit -m "feat: expose wheel competition workflow"
```

---

### Task 13: One Real PAPER CSP Proof — Human-Gated Operational Step

**Files:**
- Create at runtime: `docs/evidence/wheel-csp-proof-<timestamp>.json`
- Update: `docs/evidence/wheel-readonly-feasibility-2026-09-02.md` with proof reference only if useful.

**Interfaces:**
- Consumes: completely GREEN Tasks 1–12.
- Produces: one sanitized real PAPER evidence artifact.

- [ ] **Step 1: Verify no mutation has happened during development**

```bash
git status --short
cd backend
pytest -q
ruff check opaca tests
mypy opaca tests
```

- [ ] **Step 2: Run read-only plan first**

Generate the exact intended CSP proposal, hard-policy decision, authority result, reservation amount and final limit without submitting. Confirm one contract, PAPER, account fingerprint, 1–7 DTE, <=25% hard name exposure and current cash feasibility.

- [ ] **Step 3: If authority is APPROVAL_REQUIRED, explicitly approve that exact binding**

Do not alter policy. If AUTO, no approval record is needed.

- [ ] **Step 4: Execute exactly one PAPER CSP**

Use `wheel-submit-paper` once. Do not manually retry on timeout/UNKNOWN. Reconciliation owns recovery.

- [ ] **Step 5: Read back and reconcile**

Expected success proof:

```text
exact broker client-order id found
exact OCC order is FILLED/open as observed
matching short PUT position exists
ACTIVE assignment reservation exists
Wheel state = SHORT_PUT_OPEN
reconciliation = RECONCILED
```

If not, stop with UNKNOWN and preserve reservation.

- [ ] **Step 6: Commit sanitized evidence, never credentials**

```bash
git add docs/evidence/wheel-csp-proof-*.json
git commit -m "docs: capture real paper csp proof"
```

---

### Task 14: Submission Materials; Covered Call Only Afterward

**Files:**
- Modify: `README.md`
- Create: `docs/submission/one-page-writeup.md`
- Create: `docs/submission/demo-script.md`

- [ ] **Step 1: Update README from verified facts only**

Show architecture, risk gates, MCP read-only enforcement, real PAPER CSP proof, and limitations. Do not claim completed Wheel cycle or profit if not observed.

- [ ] **Step 2: Write one-page submission explanation**

Cover AI logic, deterministic risk gates, Alpaca MCP observation lane, Alpaca Trading API/`alpaca-py` execution, assignment-capital semantics, UNKNOWN fail-closed behavior.

- [ ] **Step 3: Write 90-second hero demo script**

Sequence: AI/MCP context -> oversized intent -> visible $32k vs $25k REJECT -> one repair -> compliant intent -> authority -> real PAPER CSP/read-back -> SHORT_PUT_OPEN + reserved capital.

- [ ] **Step 4: Only if all submission-critical material is done, decide whether to implement covered call**

Covered call is explicitly optional and must not delay submission.

- [ ] **Step 5: Final verification and commit**

```bash
cd backend
pytest -q
ruff check opaca tests
mypy opaca tests
cd ..
git diff --check
git status --short
```

Commit documentation as one logical milestone.

---

## Plan Self-Review

- Spec coverage: capital base, held-share exposure, aggregate delegation, terminal reservation release, MCP allowlist, approval TTL, 0-DTE ban, premium floor, option order identity, account binding, atomic final check, reconciliation constants/triggers and proof artifact are all assigned to tasks.
- Scope control: no UI, Greeks, spreads, optimizer, market-wide scan, live mode, broad refactor, automatic roll, exercise, or covered-call dependency before CSP proof.
- Type boundary: options remain in `opaca.wheel`; existing equity `Position`, `ProposedOrder`, `TreasuryGuardEngine` and execution service are not generalized.
- Mutation boundary: Tasks 1–12 are broker-read-only or fake-gateway tested. Task 13 is the sole real PAPER option mutation step.
