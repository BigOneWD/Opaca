# OPACA Phase 3 — final narrow remediation retest @ cd3dc86

**RED TEAM VERDICT: PASS WITH FINDINGS**
*(no execution-safety findings remain; the findings are the two pre-live readiness items reported separately, exactly as the brief asked)*

**TARGET SHA:** `cd3dc86b7153718bbc98072a79a81ae3587f9477` (`fix: close paper execution red-team findings`)
**PREVIOUS:** `79a7b1b837c86dc700533eeda6f5699b197a7d4a` — verified as the target's parent
**BASELINE:** `review/treasury-red-team @ 351af819a418d6ebb023ec7d1d2ef43019b0d405`

All three coordinates resolve exactly. Reviewed in an isolated detached worktree, clean at start and end. No production code modified, nothing merged, no findings fixed, and **no live PAPER mutation — no broker mutation of any kind — was performed.**

The diff is narrow and confined to the five findings: 15 files, +724/−19, of which 8 are production modules.

---

## GATES

| gate | result |
| --- | --- |
| builder suite | **365 passed / 2 skipped** (344 at `79a7b1b`; +21, none removed) |
| skips | both live smokes, correctly gated |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 69 files already formatted |
| `mypy --strict` | Success: no issues found in 69 source files |
| `git diff --check` | clean, exit 0 (worktree, and `79a7b1b..cd3dc86`) |
| credential scan | 98 tracked files → **1 finding, the same false positive** (`paper.py:11`, the env-var *name*); regex sweep → 0 |
| mutation capability scan | **2 call sites**, both `execution/service.py`, both on `mutate_gateway`; **0** HTTP/socket imports |
| `python -O` / `-OO` | 365 passed / 2 skipped under both |

---

## RED-TEAM TESTS

**135 passed / 0 execution-safety findings**, deterministic across three runs. Adding the pre-live readiness file: 139 passed / 4 reported items.

Whole `redteam/` tree: **765 passed / 10 failed** — 4 pre-live reports and 6 carried Phase 2 / treasury-core P2–P3 markers. No execution-safety marker remains anywhere.

**Teeth:** the current suite run against the previous commit `79a7b1b` fails **30 tests** that pass here, spread across all five findings. Every inversion is real.

---

## P1-2 DOUBLE COUNT: **CLOSED**

`_unresolved_from_broker_orders` now skips any broker order whose `client_order_id` is locally known (from reservations, unknown orders, or execution rows), and `_merge_unresolved` dedupes by identity, keeping the largest remaining quantity when two views of one order disagree.

| enumerated case | required | measured |
| --- | --- | --- |
| matching Opaca reservation + broker SELL 10 | capacity 90 | **90** — and the honest 90-share sell is authorised |
| external broker SELL 10 + independent local reservation 10 | capacity 80 | **80** — the external order alone leaves 90; adding our own distinct reservation takes it to 80 |
| matching partial-fill order (60 ordered, 20 filled) | commitment counted once | **40** counted against a real encumbrance of 40; the free 40 is authorisable |
| ambiguous / UNKNOWN identity | fail closed, no oversell | an unresolved SELL with undeterminable size drives capacity to **0**; no sell of any size is authorised. An UNKNOWN execution retains its 60 and caps capacity at ≤ 40 |

**Restart:** verified. The store is closed and reopened; the live order still correlates to its reservation by deterministic `client_order_id` (`opaca-` + sha256(proposal_id:leg_index)[:32]), capacity is 90 before and after, and a further reconcile leaves it at 90. Correlation is derived, not cached, so it cannot drift across a restart.

Two distinct commitments are still counted twice — the fix suppresses double-counting of *one* order, not aggregation of *two*.

## P1-1 ZERO-DELTA RECON: **CLOSED**

The short-circuit is gone. `symbols` now includes `set(explained)`, the comparison is `delta == expected` for every symbol, and a zero delta against a non-zero expectation gets its own reason string.

| enumerated case | result |
| --- | --- |
| real no-change | `RECONCILED` |
| expected fill reflected | `RECONCILED` |
| **expected fill absent** | `DRIFT_DETECTED` — *"SGOV locally explained fill -10 not reflected in broker position"* |
| fill exactly offset by external movement | `DRIFT_DETECTED` (end to end) |
| duplicate broker snapshot | three identical reconciles stay `RECONCILED`; a subsequent second, unexplained −10 is `DRIFT_DETECTED`, so a stamped fill cannot explain a second delta |
| progressive partial fills (10 → 20 → 30) | each step `RECONCILED`, reservation ends at 0 |

**Net delta == 0 no longer implies RECONCILED**, asserted directly: identical inputs give `RECONCILED` with nothing explained and `DRIFT_DETECTED` with a 10-share fill explained.

## P1-3 STRANDED LEGS: **CLOSED**

New terminal state `NOT_SUBMITTED`, `_abort_unsent_legs`, an `ORDER_NOT_SUBMITTED` audit event, and a `sync_proposal_reservations` call afterwards.

Tested on a three-leg proposal with the rejection at the first, middle and final leg:

* **zero broker submit calls** for every leg after the rejection (`submit_calls == rejected_leg` exactly);
* later legs reach **`NOT_SUBMITTED`**, an explicit terminal state — never `UNKNOWN_REQUIRES_RECONCILIATION`, and always with `broker_order_id is None`;
* **audit reason preserved**: one `ORDER_NOT_SUBMITTED` per stranded leg, carrying *"upstream leg … rejected by broker"* and the correct `client_order_id`;
* **reservations released** for all three symbols;
* a final-leg rejection strands nothing;
* `NOT_SUBMITTED` is absorbing — every transition out of it raises;
* retrying a proposal with stranded legs never resubmits.

**The uncertainty contrast holds.** With a lost response instead of a rejection, leg 0 is `UNKNOWN_REQUIRES_RECONCILIATION` and its reservation is retained (5 SGOV, 2 BIL still held), while legs 1 and 2 — which the loop provably never dispatched — are `NOT_SUBMITTED`. The distinction is enforced structurally: `_mark_not_submitted` refuses to mark anything that is not `SUBMITTING` with a null `broker_order_id`, verified by attempting it on a leg that reached the broker and getting `ExecutionInvariantError`.

## P2-1 BARE ASSERT: **CLOSED**

AST scan across **all 40 production modules** in `backend/opaca/`: **0 `ast.Assert` nodes**. The guard is now `raise ExecutionInvariantError(...)`, plus an explicit empty-legs check. Full suite green under both `python -O` and `python -OO` (365 passed / 2 skipped each).

## P3-1 FINAL KILL SWITCH: **CLOSED**

The switch is now read four times per execution — three inside the intent transaction (twice directly, once via `build_policy_context`) and once inside `_submit_leg`, immediately before the broker call.

| case | required | measured |
| --- | --- | --- |
| **A** — flip before revalidation | 0 submits | 0 submits, blocked *"kill switch active"*, no execution rows persisted |
| **B** — flip after reconcile, before the intent transaction | 0 submits | 0 submits, blocked, no execution rows persisted |
| **C** — flip after the intent is persisted, immediately before submit | 0 submits | 0 submits; leg is `NOT_SUBMITTED` with `broker_order_id is None`; `ORDER_NOT_SUBMITTED` **and** `EXECUTION_BLOCKED` audited; result blocked with *"kill switch active immediately before submit"*; capacity released |
| **D** — flip after the broker mutation was attempted | normal ACK / UNKNOWN; must not pretend nothing was sent | the order is acknowledged (`FILLED`), `submitted=True`, and the state is **never** `NOT_SUBMITTED`. With a lost response it recovers as `UNKNOWN_REQUIRES_RECONCILIATION` with the reservation retained |

The read is per-leg, so a multi-leg proposal stops mid-flight: flipping after leg 0's submit leaves legs 1 and 2 `NOT_SUBMITTED` with `submit_calls == 1`.

---

## CRITICAL REGRESSIONS

| area | verdict |
| --- | --- |
| **DUPLICATE ORDER SAFETY** | **PASS** — five retries, four concurrent executors on separate connections, a broker-reported duplicate `client_order_id`, and a crash between intent and submit all end at `submit_calls == 1` with one execution row |
| **UNKNOWN RECOVERY** | **PASS** — timeouts on both sides of the accept, an unavailable lookup, and an order absent at the broker all hold `UNKNOWN` with capacity retained; repeated recovery is idempotent; restart recovery covers every open proposal and never resubmits |
| **CURRENT ELIGIBILITY** | **PASS** — Phase 2's `is_auto`-on-replay contract intact (the whole 417-test Phase 2 suite still green) |
| **SELL RESERVATION** | **PASS** — full fill releases, partial fill resizes to the exact remainder, cancel-after-partial releases only the dead remainder, rejection releases, no negative quantities; the oversell attack (60 of 100, fill, then 60 again) still fails |
| **BUY RESERVATION** | **PASS** — cash reservation releases on fill and resizes on partial fill to the exact residual notional |
| **PARTIAL FILLS** | **PASS** — progressive 10 → 20 → 30 fills reconcile at each step and end with zero reservation |
| **SETTLEMENT IDEMPOTENCY** | **PASS** — one event per increment; five recoveries do not duplicate proceeds; totals equal the filled notional exactly; a BUY creates no proceeds |
| **T+1 LIQUIDITY** | **PASS** — paper credits the proceeds instantly and the derived schedule excludes them: `settled_cash` stays at 100,000 on trade date with `unsettled_total` equal to the proceeds, and rises by exactly the proceeds on the settlement date. Settlement strictly increases deployable cash, by exactly 60 × 100.69 in the larger case |
| approval hash / expiry | **PASS** — grant bound to proposal *and* payload hash, idempotent, refused for unknown or expired proposals, never overrides a hard REJECT |
| paper-only gateway | **PASS** — live endpoint, empty endpoint, localhost, arbitrary host and the look-alike `paper-api.alpaca.markets.evil.com` all refused; ten extra mutator names and a nested mutable client each rejected |
| Treasury Core | **PASS** — 214 probes green |
| Phase 2 | **PASS** — full suite green; no control regressed |

---

## REMAINING P0

**None.**

## REMAINING EXECUTION-SAFETY P1

**None.** All five findings are closed and nothing new surfaced in the retest.

## REMAINING FAIL-OPEN ISSUE

**None.** Six carried P2/P3 items remain from earlier phases, all fail-closed and all recorded in `docs/backlog.md`: the bound-method `__self__` introspection escape; three store mutators outside transactions; a COMMIT failure leaving the connection in a transaction; leg-order-sensitive `proposal_hash`; the mutation scan excluding `spike/`; the non-hermetic test gate.

---

## LIVE PRICE READINESS: **PRE-LIVE BLOCKER**

Not fixed here, as instructed. Three pieces of evidence:

1. **No market-data client exists anywhere in the package.** No `get_latest_trade`, `get_latest_quote`, `StockHistoricalDataClient`, or any market-data import in any of the 40 modules. Prices are a caller-supplied `Mapping[str, Decimal]` on both `evaluate_and_reserve` and `execute_reserved_proposal`.
2. **The only path that would place a real order prices it from test constants.** `test_live_paper_mutation.py` passes `tests.helpers.DEFAULT_PRICES` to *both* safety paths and uses `DEFAULT_PRICES["SGOV"]` as the leg's `reference_price`. Those are hard-coded: SGOV 100.69, a fill price observed on 2026-08-28; BIL 92.00 and SHV 110.00 are documented in the source as *"fixed deterministic constants"* with no evidence basis.
3. **A wrong price flips the authority decision, it does not merely mis-report size.** The same 300-share BUY is 30,207.00 of exposure at the real price — `APPROVAL_REQUIRED`, `is_auto=False` — and 3,000.00 at a wrong one: **`AUTO`, `is_auto=True`**. And nothing binds the two price inputs together: a 100-share BUY carrying `reference_price = 0.01` against a market price of 100.69 yields a notional of 1.00 and **fails no check at all**. `reference_price` drives CHECK-01/06/07/11/14 and the delegated-authority limits; `context.prices` drives CHECK-04 concentration; no check compares them.

`reference_price` is also the settlement-proceeds fallback in `_avg_price` when the broker omits `filled_avg_price`, so a stale price would propagate into the T+1 liquidity schedule.

The exposure of the *specific* smoke (BUY 1 SGOV) is about $100 and would be rejected by the broker if unaffordable. The blocker is the mechanism, not that order: **a real PAPER mutation currently derives its authority decision from a synthetic constant.** Before any live paper mutation, a real quote must feed both `prices` and `reference_price`, and a check should bind them to each other within a tolerance.

## SCHEMA V2 READINESS: **FRESH-DB REQUIRED**

`SCHEMA_VERSION = 2`. A fresh database bootstraps cleanly — WAL on, foreign keys on, `execution_orders` and `approval_grants` present.

An existing v1 database **fails closed on open**: `bootstrap()` compares `MAX(version)` to `SCHEMA_VERSION` and raises `PersistenceError: unsupported schema version 1; expected 2 (fail closed)`. There is no migration step — no `ALTER TABLE` anywhere in `opaca/persistence/`, only the equality check.

Failing closed is the right default and no data is corrupted. But any existing v1 state — proposals, reservations, autonomous history, audit trail — is unreachable without an explicit migration. If no v1 database matters, this is **FRESH-DB REQUIRED** and nothing more is needed. If audit continuity across the phase boundary matters, it becomes MIGRATION REQUIRED and should be written before the first v2 database is created in anger.

---

## MERGE RECOMMENDATION: **MERGE**

All five findings are closed, each verified by tests that fail at the previous commit — 30 of them. Nothing regressed: the builder suite grew 344 → 365 with no test removed, the mutation surface is still two call sites, and the full 765-test red-team tree shows no execution-safety marker anywhere. Static gates, the credential scan and the mutation capability scan are clean, and everything passes with assertions stripped — which now means something again, since there are no assertions left to strip.

The remediation is also notably well-shaped: `NOT_SUBMITTED` is a real state with a real invariant behind it, not a flag, and `_mark_not_submitted` refuses to be applied to anything that reached the broker.

**Two gates before the first live PAPER mutation, neither of which blocks this merge:**

1. **Live prices.** Wire a real quote into both `prices` and `reference_price`, and add a check binding them. Until then the live mutation smoke should stay unrun — its authority decision is computed from a 2026-08-28 constant.
2. **Schema.** Decide fresh-DB versus migration before a v2 database accumulates state worth keeping.

And the standing item from the previous review: **nothing in this layer has met a real broker.** The adapters have never seen a live Alpaca fill payload, and `AlpacaPaperExecutionGateway` still detects a duplicate `client_order_id` by string-matching the exception text. That path degrades safely to `UNKNOWN`, but the read-only live smoke should be run before the mutating one.

**Not merged. No production code modified. No broker mutation performed.**

---

*Artefacts: detached worktrees at `cd3dc86` and `79a7b1b` (session-local, outside the repository), and the Phase 3 suite at `redteam/paper_execution_79a7b1b/` — now 143 tests, with the five FINDING markers inverted, a 36-test `test_retest_cd3dc86.py`, and `test_prelive_readiness.py` carrying the two readiness answers. Preserved on `review/treasury-red-team`.*
