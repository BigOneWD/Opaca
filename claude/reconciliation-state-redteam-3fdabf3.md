# OPACA Phase 2 red-team review — reconciliation state + SQLite atomic reservation

**RED TEAM VERDICT: PASS WITH FINDINGS**

**TARGET:** `3fdabf3bcbe3e0d8c8ccfb5a9feedad584c4b6e2`

`git rev-parse origin/feat/reconciliation-state` → `3fdabf3bcbe3e0d8c8ccfb5a9feedad584c4b6e2` — exact match.
`git rev-parse origin/main` → `b940c71959827211d72954cf65dcf0ccec2b7b93` (tag `treasury-core-complete`) — exact match.

Reviewed in an isolated detached worktree at the target SHA. No production code modified, nothing merged, nothing pushed, nothing committed. Worktree clean at start and end.

Toolchain rebuilt to the pins in `backend/requirements-dev.txt`: CPython 3.11.15 · pytest 9.1.1 · ruff 0.16.5 · mypy 2.3.1 (compiled).

The diff against `main` is additive: 4,163 insertions across 31 files, of which the only pre-existing files touched are `backend/opaca/__init__.py` (+7/−2), `backend/pyproject.toml`, `.gitignore` and `docs/SPEC.md`. **No Treasury Core policy file was modified.** Verified with `git diff --stat b940c719..3fdabf3`.

---

## BUILDER GATES

| gate | claimed | measured |
| --- | --- | --- |
| full suite | — | **273 passed / 1 skipped** |
| Treasury Core regression (14 pre-existing test files) | 230 passed | **230 passed / 0 failed** |
| new tests (7 new test files) | 43 passed / 1 live-paper skipped | **43 passed / 1 skipped** |
| concurrency (`test_atomic_reservation.py`) | PASS | **5 passed** |
| idempotency | PASS | **PASS** (see P0-D) |
| mutation scan (`test_audit_and_mutation_scan.py`) | PASS | **4 passed** |
| `ruff check .` | — | All checks passed |
| `ruff format --check .` | — | 58 files already formatted |
| `mypy --strict` | — | Success: no issues found in 58 source files |
| `git diff --check` | — | clean, exit 0 (worktree vs HEAD, and `b940c719^..3fdabf3`) |
| credential scan | — | detect-secrets 1.5.0 over 85 tracked files → **1 finding, a false positive** (`paper.py:11`, the literal `ENV_SECRET = "APCA_API_SECRET_KEY"` — an env-var *name*). Independent regex sweep for `APCA_*=`, `PK[A-Z0-9]{16,}`, `secret_key=`, PEM headers → **0**. The 17 evidence JSONs contain no `account_number`, `api_key` or `secret`. |
| broker mutation scan | — | **0 mutation call sites in `backend/opaca/`** (AST, not grep). One outside it — see P2-4. |
| `python -O` / `-OO` | — | 273 passed / 1 skipped under both; **0 bare `assert` statements in `backend/opaca/`** (AST scan) |

Per-file new-test counts: reconciliation 9 · persistence 5 · atomic reservation 5 · broker gateway 12 · failure modes 8 · audit+mutation scan 4 · live paper 1 (skipped).

**Every builder claim in the brief reproduces exactly.** WAL, `foreign_keys`, `BEGIN IMMEDIATE`, the READ → RECONCILE → EVALUATE → RESERVE → PERSIST ordering and the absence of any broker execution path are all confirmed independently below.

---

## RED-TEAM TESTS

**190 passed / 21 findings**, deterministic across three consecutive runs, and identical under `python -O`.

Every one of the 21 "failures" is a deliberate `pytest.fail("FINDING …")` marker placed *after* its invariant assertions held; the failure message is the finding. `-k "not FINDING"` gives the pure invariant suite: **190 passed, 21 deselected**.

The suite is at `redteam/reconciliation_3fdabf3/` on `review/treasury-red-team` (written, **not committed**).

### Probe teeth

`test_teeth.py` neutralises the reservation mechanism (`sell_reservations` → empty, `_cash_reservation_obligations` → empty) and re-runs the same invariant assertions. With the mechanism removed:

* two concurrent SELL 60 proposals **both** reach AUTO and reserve **120 shares of a 100-share position**;
* two concurrent 15,002.81 BUY proposals deploy **30,005.62 against 22,000** of deployable cash.

Both teeth checks pass, so the green P0-A and P0-B results are detections, not vacuous passes.

---

## P0 FINDINGS

### P0-1 — the idempotent-replay branch returns `is_auto=True` with every safety gate skipped *(latent fail-open)*

`evaluate_and_reserve()` handles a duplicate `proposal_id` **first**, before the reconciliation-status gate, before the stale-snapshot gate, and before any re-evaluation. It returns

```python
reserved = existing.status is ProposalRecordStatus.AUTO_AUTHORIZED
```

so `OrchestrationResult.is_auto` is `True` on replay whatever the current state is. Four independently reproduced cases:

| probe | condition at replay time | result |
| --- | --- | --- |
| `test_FINDING_replay_bypasses_the_stale_snapshot_gate` | snapshot advanced v1 → v2, caller passes the **stale** v1 | `is_auto=True`, `snapshot_version=1`, **no `STALE_SNAPSHOT` audit written** |
| `test_FINDING_replay_reports_auto_while_state_is_drifted` | latest reconciliation is `DRIFT_DETECTED` | `is_auto=True`, `blocked=False` |
| `test_FINDING_replay_reports_auto_while_state_requires_review` | latest reconciliation is `UNKNOWN_REQUIRES_REVIEW` | `is_auto=True` |
| `test_FINDING_replay_reports_auto_while_kill_switch_is_active` | kill switch **ACTIVE** | `is_auto=True` (CHECK-00 is never re-run) |

This directly contradicts three requirements in the brief: *"Unknown must never become an executable state"*, *"reservation denied and fresh reconciliation/evaluation required"*, and *"no future execution may rely on the old decision."*

**Currently unexploitable** — no submission path exists, and the replay is capacity-neutral (verified: no extra reservation row, no extra `autonomous_executions` row, aggregate reserved quantity unchanged across five replays). But `is_auto` is this layer's entire authorization contract, and the execution phase is precisely the caller that will key off it. **This must be closed before any executor is written.**

Fix shape: run the reconciliation-status, staleness and kill-switch gates *before* the duplicate lookup, and have the replay return the stored decision with `reserved=False` unless the state it was made against is still current.

### P0-2 — no maximum snapshot age; the staleness guard is opt-in

Two independent gaps, both reproduced:

* `test_c08_FINDING_no_snapshot_age_bound` — a `RECONCILED` snapshot never expires. Reconcile at T, then call `evaluate_and_reserve(now=T+30 days, expected_snapshot_version=<same version>)` → **AUTO, reservation created**. Nothing anywhere compares `snapshot.captured_at` or `broker.as_of` to `execution.now`. Note also that `adapt_account` stamps `as_of = now` (the local read time), not a broker-supplied timestamp, so the snapshot carries no independent broker clock to age against.
* `test_c09_FINDING_expected_snapshot_version_is_optional` — `expected_snapshot_version` defaults to `None`, which disables the version gate entirely. A caller that evaluated against snapshot N and then calls `evaluate_and_reserve` without the argument reserves against N+k silently.

The staleness invariant therefore holds only for callers who both (a) reconcile immediately beforehand and (b) pass the version. `read_reconcile_evaluate_reserve()` does both and is safe; `evaluate_and_reserve()` is exported from `opaca.orchestration` and is not.

Fix shape: make `expected_snapshot_version` required, and add a policy-driven `max_snapshot_age_seconds` checked inside the transaction.

---

## P1 FINDINGS

### P1-1 — the "read-only" gateway wraps a fully mutable `TradingClient`

`AlpacaPaperGateway.__init__` stores the raw alpaca-py `TradingClient` on `self._client`. `assert_read_only_gateway()` inspects only the *wrapper's* own attributes, so it passes while `gateway._client.submit_order(...)`, `.cancel_order_by_id(...)` and `.close_all_positions(...)` all remain reachable from any holder of the gateway. Reproduced: `test_FINDING_readonly_wrapper_retains_a_fully_mutable_trading_client` calls `gateway._client.submit_order("anything")` through a gateway that has just passed the read-only guard.

Compounding it, the guard is a **name blacklist** and misses alpaca-py's actual method names: `FORBIDDEN_BROKER_MUTATIONS` contains `cancel_order` and `replace_order`, but alpaca-py exposes **`cancel_order_by_id`** and **`replace_order_by_id`**. A gateway exposing either passes `assert_read_only_gateway()` outright (`test_FINDING_forbidden_name_list_misses_real_alpaca_py_method_names`).

The brief anticipated exactly this: *"A read-only interface that internally exposes a generic mutable TradingClient is worth flagging even if currently unused."*

What I *can* prove: an AST scan of all 26 files in `backend/opaca/` finds **zero** attribute accesses or calls named `submit_order`, `cancel_order*`, `replace_order*`, `close_position`, `close_all_positions`, `exercise_options_position`, `.post(`, `.put(`, `.patch(`, `.delete(`, `.request(`; **zero** imports of `requests`, `httpx`, `urllib`, `http`, `aiohttp` or `socket`; and no dynamic dispatch onto a broker object (the only computed `getattr` sites are the guard iterating its own name set, and `engine.py` dispatching its own `_check_NN` handlers on `self`). The `alpaca` import is lazy and no order-request type is imported. So **the phase path as written cannot submit** — but that is a property of the current call graph, not of the encapsulation, and it is one attribute access away from being false.

Fix shape: hold the client behind a closure or `__slots__`-free private module, expose only bound read methods, and validate the *wrapped* object against a method allowlist rather than a name blacklist.

### P1-2 — corrupt broker records escape reconciliation as raw exceptions instead of `INVALID_BROKER_STATE`

Three cases, all fail-closed but none classified:

* **Duplicate broker position rows** → `sqlite3.IntegrityError` propagates straight out of `reconcile()` (`UNIQUE(snapshot_id, symbol)`); no snapshot is written, no status is returned.
* **Duplicate broker `client_order_id`** → same, via `UNIQUE(snapshot_id, client_order_id)`.
* **`filled_quantity > quantity`** → `adapt_order_snapshot` does not validate the relationship, so the contradiction **persists with status `RECONCILED`**. It only surfaces later, as a raw `ValueError` out of `evaluate_and_reserve` when `build_policy_context` constructs the `UnresolvedOrder` (whose `__post_init__` does validate). `_unresolved_from_unknown` does not wrap it in `InvalidBrokerStateError` the way `adapt_unresolved_order` does.

Consequence: the reconciliation state machine has five documented outcomes, and three real corrupt-broker inputs land in none of them. Callers cannot distinguish "broker is corrupt" from "the store is broken", and the audit trail records nothing.

### P1-3 — `APPROVAL_REQUIRED` expiry is recorded but never enforced

`persist_proposal_decision` writes `approvals(expires_at, snapshot_version, payload_hash)` correctly, and I confirmed no code anywhere reads or updates the `approvals` table (one `INSERT INTO approvals`, zero `FROM approvals`/`UPDATE approvals`). But replaying the same proposal **seven days after** its 300-second expiry returns the stored `APPROVAL_REQUIRED` decision verbatim via the idempotent-replay branch, with no freshness check. Same root cause as P0-1.

On the positive side, everything else in P1-F holds: `APPROVAL_REQUIRED` creates **zero** reservations, **zero** cash reservations and **zero** `autonomous_executions` rows; a hard `REJECT` is stored as `REJECTED` with `expires_at = NULL` and no `approvals` row; and a kill-switch `REJECT` is likewise unapprovable.

### P1-4 — detection gap: unexplained broker hold-aside produces no drift

`compare_state` builds `reserved_by_symbol` from local reservations and then iterates **that** dict. With no local reservation, the `quantity_available` consistency check never runs. A broker reporting `qty=100, qty_available=10` with no open orders and no local reservation reconciles as **`RECONCILED`** (`test_FINDING_unexplained_broker_hold_aside_without_local_reservation`). 90 shares are encumbered at the broker for reasons Opaca cannot explain, and nothing is flagged.

Not fail-open — CHECK-16 still bounds sells by `min(quantity_available, quantity − reserved)`, so it cannot oversell — but it is exactly the class of divergence reconciliation exists to catch.

### P1-5 — `seed_scenario_once()` is not transactional when called without a connection

`reconcile()` always passes `conn`, so the production path is atomic. Called directly on the store's `isolation_level=None` connection, the `scenario_state` insert commits on its own before the obligation inserts run. Reproduced (`test_FINDING_direct_seed_without_transaction_is_not_atomic`): with an `obligation_id` collision injected, the scenario row is committed and the obligations fail — leaving an authoritative-looking seed with the wrong obligations, and no way to re-seed because `get_scenario()` now returns a row.

The same shape applies to `insert_settlement_event`, `set_kill_switch` and `record_audit`: each is an independent commit boundary on the autocommit connection, so any multi-statement caller has no atomicity unless it opens `begin_immediate()` itself (`test_FINDING_store_mutations_outside_transactions_autocommit`).

---

## P2 FINDINGS

1. **Reservations are never released.** No `UPDATE reservations`, no `DELETE FROM reservations`, no `RELEASED` status, no expiry anywhere in the package. Sell capacity and deployable cash are consumed permanently for the life of the database file.
2. **One AUTO sell permanently locks out every later BUY of that symbol.** Consequence of (1): the never-released SELL reservation is surfaced to CHECK-10 as an opposing unresolved order, so after a single AUTO sell of SGOV, every subsequent SGOV BUY is a hard `REJECT` — forever. Reproduced. For a bot meant to trade daily this is a self-inflicted denial of service, not just a conservatism.
3. **`proposal_hash` is sensitive to leg list order.** It canonicalises JSON keys (`sort_keys=True`) but iterates `proposal.legs` in list order, and `Proposal.__post_init__` does not sort. The same logical proposal retried with its legs listed in a different order hashes differently and is rejected as `"proposal_id reused with a different payload"` — permanently unreplayable. Fail-closed, but it makes retry idempotency depend on caller-side list ordering.
4. **Mutation-scan scope is the package, not the repository.** `spike/spike.py` (tracked at this SHA, unchanged from `main`) calls `client.submit_order(request)` at line 388 and `client.cancel_order_by_id(order_id)` at line 429 on a live paper `TradingClient` built from `APCA_*` environment credentials. Pre-existing and outside `backend/opaca/`, so the "no broker execution exists" claim is true as scoped — but the scope should be stated explicitly.
5. **Test gate still not hermetic** (carried over from treasury-core). `backend/tests/helpers.py` resolves `REPO_ROOT/spike/evidence` at import time, so `backend/` is not independently testable.
6. **A COMMIT failure leaves the connection inside a transaction.** `begin_immediate()` issues `ROLLBACK` only from its `except` branch; if `COMMIT` itself raises, no rollback is issued. Nothing durable leaks — a reopened database shows byte-identical row counts — but the uncommitted rows stay readable on that connection and the next orchestration call on it fails with `SqliteBusyError`. Fail-closed; worth a `finally`-style rollback.
7. **Corrupt policy rows escape as raw `MoneyError`.** An out-of-range `policies` value (e.g. `per_order_autonomous_notional_max = 0`) propagates a raw `MoneyError` out of `evaluate_and_reserve` rather than a blocked `OrchestrationResult`. Rolls back cleanly; different shape from every other failure mode.
8. **`test_live_paper_readonly.py` is entirely bare `assert`s**, so under `python -O` with `--live-paper` it would pass vacuously. It also does not inspect the wrapped client (P1-1).
9. **Deterministic `client_order_id` collisions** across different proposals surface as a raw `sqlite3.IntegrityError` from `order_identity`'s primary key. Correctly fail-closed and atomic (verified: no proposal row, no reservation, no authority consumption), but again unclassified.

---

## AREA VERDICTS

**ATOMIC SELL RESERVATION: PASS.** Every attack variant holds the invariant *aggregate active reservations ≤ reconciled position*, with teeth proven. 60+60 → exactly one AUTO. 50+50 → both, correctly, totalling exactly 100. 50 + 50.000000001 → exactly one. 30×4 → at most three, aggregate ≤ 100. 100+100 → one. 0.000000001 + 100 → one. Same `proposal_id` twice concurrently → one reservation, one authority consumption. TOCTOU probe (one worker held 1 s between `decide()` and `persist_reservations`) → one AUTO, aggregate ≤ 100. **Two real OS processes** with separate connections → exactly one AUTO, 60 reserved. Loser always `reserved=False`, zero reservation rows, zero authority consumption. Busy/locked database → `SqliteBusyError`, never AUTO, nothing written. Rollback after reservation insert → nothing, in-memory and after reopen. The `BEGIN IMMEDIATE` write lock plus the reservation-as-`UnresolvedOrder` feedback into CHECK-16 genuinely serialises this.

**ATOMIC CASH RESERVATION: PASS.** With the exact scenario in the brief — opening cash 100,000 seeding reserve 40,000 and obligations 38,000, leaving **22,000** deployable — two concurrent 15,002.81 BUY proposals produce exactly one AUTO and aggregate deployment ≤ 22,000. Same for 3 and 4 concurrent buyers, and for the sequential control. The mechanism is real: an AUTO buy writes a `CASH_DEPLOYMENT` reservation which `build_policy_context` re-injects as an obligation due today, tightening `investable_cash` (CHECK-01) and `funding_ceiling` (CHECK-06). Rolling authority also holds under concurrency: three ~20,138 concurrent sells consume ≤ 50,000 of the 24 h notional limit, and nine concurrent sells produce ≤ 6 executions against the runaway hourly cap. Inflated `buying_power` (4×) and inflated `non_marginable_buying_power` are ignored, not trusted.

**STALE SNAPSHOT: FAIL.** The version gate itself works — position change, cash change and a new unresolved broker order each advance the version and block the old decision; a non-`RECONCILED` latest snapshot blocks even when the version matches; settlement events, policy rows and the kill switch are all read inside the reserving transaction. But three bypasses defeat the guarantee: no maximum snapshot age (P0-2), `expected_snapshot_version` optional (P0-2), and the replay branch skipping the gate entirely (P0-1).

**IDEMPOTENCY: PASS.** The stated invariant — *a retry must never increase executable capacity* — holds in every attack. Exact retry after commit ×5: identical reservation count, authority history, proposal legs and `order_identity` rows. Retry after rollback: a clean first attempt, one reservation, one execution. Retry while the first transaction is open (threads): serialised, one reservation, one execution. Same ID with different content: blocked, not replayed, no capacity change. Replay of a REJECTED proposal: still rejected, zero reservations. Duplicate reservation insert: refused by `idx_reservations_active_sell`. `client_order_id` collision: fails closed, atomic. The *verdict* returned by a replay is the problem (P0-1), not the capacity.

**SCENARIO SEED-ONCE: PASS.** Seeded at 99,999.99 → reserve 39,999.99, obligations 37,999.98 (payroll 23,999.99 + suppliers 13,999.99), each rounded DOWN. Later broker cash of 80,000 / 150,000 / 500,000 / 0.01 / 99,999.98, applied repeatedly, never rescales anything; exactly one `scenario_state` row and exactly one `SCENARIO_SEEDED` audit event survive. Survives close/reopen and process restart. Four concurrent initialisers racing with four different cash values produce one seed, one audit event, two obligations. A crash during the first seed transaction leaves no scenario and no snapshot, and the next reconcile seeds correctly from the *then-current* cash. A non-`RECONCILED` first pass does not seed. One P2 on the direct non-transactional API (P1-5).

**SQLITE DURABILITY: PASS.** WAL survives reopen; `foreign_keys` is ON for the main connection, for `connect_writer()` and for every re-opened `SQLiteStore`, and is genuinely enforced (an orphan reservation insert raises `IntegrityError`). Rollback removes every related row across all eight tables, in memory and after reopen. Commit persists all of them — proposal, 1 leg, 17 policy checks, authority decision, 2 reservations, order identity, authority execution. `Decimal` round-trips exactly for nine boundary values including `1E-9`, `-1E-9`, `-0.01` and a 24-digit integer part, both through the codec and through the database. Timestamps come back tz-aware with a zero UTC offset and compare equal to the original. Bootstrap is idempotent across five reopens (one migration row, 11 policy rows). Schema versions 0, 2, 99, −1 and an empty `schema_migrations` all fail closed with `PersistenceError`. Eight concurrent writers leave `PRAGMA integrity_check = ok` and an empty `foreign_key_check`. Failure injected at eight boundaries — before BEGIN, after BEGIN, before/after proposal persistence, before/after reservation persistence, before audit, at policy load — plus before COMMIT and at COMMIT itself: in every case the durable row counts are byte-identical to the pre-attempt state and there are zero orphan reservations. Implicit autocommit boundaries do exist (P1-5, P2-6) but leak nothing durable.

**READ-ONLY ALPACA GUARANTEE: FAIL.** No call site exists — proven by AST, not grep, across the whole package (see P1-1 for the full scan). The paper-endpoint gate is real and checks the constructed client's `_base_url`, not just a config flag: a live endpoint, a blank endpoint and a paper endpoint with `_paper=False` are each rejected. The protocol surface, the fake and `AlpacaPaperGateway` all pass `gateway_methods_are_read_only`, a gateway carrying `submit_order` is rejected by the guard, and the orchestrator turns such a gateway into `INVALID_BROKER_STATE` rather than proceeding. But the *guarantee* — structural impossibility — does not hold: the wrapper retains a fully mutable `TradingClient` on `_client`, and the blacklist misses `cancel_order_by_id` / `replace_order_by_id`. I cannot prove the phase path *cannot* submit; only that it currently *does not*.

**RECONCILIATION FAIL-CLOSED: FAIL (replay path only).** Every *fresh* evaluation is fail-closed, and the five states are genuinely distinct — `BROKER_UNAVAILABLE` and `INVALID_BROKER_STATE` return no snapshot at all; an unmapped Alpaca status, a locally-submitted order absent at the broker, a failed UNKNOWN lookup and an unavailable UNKNOWN lookup all reach `UNKNOWN_REQUIRES_REVIEW`; a broker unresolved order unknown locally, cash inconsistent with unsettled proceeds, a local reservation with no broker position, and `quantity_available` inconsistent with an existing reservation all reach `DRIFT_DETECTED`; a broker partial fill never reconciles into an executable state; a canceled-with-remainder order is correctly *not* unresolved. 66 malformed-payload probes across account, positions, assets, orders, prices and timestamps produced **zero** AUTOs — including NaN/sNaN/±Infinity/float/bool/`None`/malformed-string cash, negative and `quantity_available > quantity` positions, inactive and non-tradable assets, unmapped and unknown order states, duplicate and stranger orders, and missing/zero/negative/NaN/float/huge prices. An aware but non-UTC clock fails closed to `INVALID_BROKER_STATE`. The failure is P0-1: on the replay path, `UNKNOWN_REQUIRES_REVIEW` and `DRIFT_DETECTED` do not suppress `is_auto=True`.

---

## LIVE PAPER READ-ONLY SMOKE

`APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` are both absent from the review environment. **No credentials were requested and no live call was made.** `tests/test_live_paper_readonly.py` correctly skips (`live paper smoke not requested`) and is guarded by `--live-paper`. No preserved Mac-mini live-smoke evidence for *this* phase exists in the repository; the 17 files in `spike/evidence/` are Phase −1B artefacts dated 2026-08-28, unchanged from `main`. **No mutation was performed.**

---

## HIGHEST-RISK REMAINING UNTESTED ASSUMPTION

**That the local reservation ledger stays a faithful proxy for broker state once orders actually exist at the broker.**

Every guarantee proven above is an invariant over *local* rows. The reservation is a purely local claim: it is written, it is fed back into CHECK-16 as a synthetic `AUTO_AUTHORIZED` unresolved order — and then nothing ever reconciles it against a real order, because no order is ever submitted. The layer has never observed the sequence it exists to protect: reserve → submit → partially fill → re-reconcile → release. The two behaviours that fall out of that gap are already visible statically — reservations that are never released (P2-1) and the resulting permanent CHECK-10 lock-out (P2-2) — and both only become *safety*-relevant, rather than availability-relevant, once fills start arriving.

Second, and specific to this commit: **`OrchestrationResult.is_auto` is the contract the execution phase will consume, and it is not currently a safe signal** (P0-1). Nothing tests it as an authorization verdict because nothing yet acts on it.

---

## MERGE RECOMMENDATION: FIX THEN RETEST

The atomic reservation layer this phase exists to build **works**, under true thread and process concurrency, for both sell quantity and deployable cash, with teeth verified. The Treasury Core regression is intact at 230, the static gates and credential scan are clean, and everything passes with assertions stripped. Nothing here can currently place a trade.

Three items should close before merge, because each of them is a property the *next* phase will build on:

1. **P0-1** — move the reconciliation-status, staleness and kill-switch gates ahead of the duplicate-proposal branch; a replay must not report `is_auto=True` against drifted, unknown, stale or killed state.
2. **P0-2** — make `expected_snapshot_version` required and add a maximum snapshot age enforced inside the transaction.
3. **P1-1** — stop retaining a mutable `TradingClient` on the read-only gateway, and validate the wrapped object against an allowlist rather than a name blacklist that misses `cancel_order_by_id` and `replace_order_by_id`.

P1-2 through P1-5 and the P2 list are worth scheduling but do not block: all are fail-closed, and P2-1/P2-2 are availability rather than safety.

**No broker execution path may be added until P0-1 and P0-2 are closed.**

**Not merged. Not pushed. No production code modified. Nothing committed.**

---

*Artefacts: a detached worktree at `3fdabf3` (session-local, outside the repository) and a 211-test independent probe suite preserved at `redteam/reconciliation_3fdabf3/` on `review/treasury-red-team`. As in the previous retest, the detached worktree registered under `.git/worktrees` could not be pruned (the mount denies unlink); it is a stale entry only and holds no repository state.*
