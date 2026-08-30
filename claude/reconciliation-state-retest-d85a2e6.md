# OPACA Phase 2 — final remediation retest @ d85a2e6

**RED TEAM VERDICT: PASS WITH FINDINGS**

**TARGET SHA:** `d85a2e62b5d4c3852dcd5322eb4d2c907fbec32e`

`git rev-parse origin/feat/reconciliation-state` → `d85a2e62b5d4c3852dcd5322eb4d2c907fbec32e` — exact match. One commit above the reviewed baseline `3fdabf3` (`fix: close reconciliation state red-team findings`). Red-team evidence branch `review/treasury-red-team @ 4de3902`.

Reviewed in an isolated detached worktree at the target SHA. No production code modified, nothing merged, no broker execution added, nothing committed. Worktree clean at start and end.

Toolchain unchanged and rebuilt to the pins in `backend/requirements-dev.txt`: CPython 3.11.15 · pytest 9.1.1 · ruff 0.16.5 · mypy 2.3.1 (compiled).

Remediation diff is 19 files, +1,410 / −238: 11 production files under `backend/opaca/`, 7 test files, and `docs/reconciliation-state.md`. No Treasury Core policy file was touched.

---

## BUILDER TESTS

| gate | result |
| --- | --- |
| full builder suite | **303 passed / 1 skipped** (273 at `3fdabf3`; +30, none removed or relaxed) |
| Treasury Core regression (14 pre-existing files) | **230 passed / 0 failed** — unchanged |
| Phase 2 files (7 files) | **73 passed / 1 skipped** (43 + 1 at `3fdabf3`) |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 58 files already formatted |
| `mypy --strict` | Success: no issues found in 58 source files |
| `git diff --check` | clean, exit 0 (worktree vs HEAD, and `3fdabf3..d85a2e6`) |
| credential scan | detect-secrets 1.5.0 over 85 tracked files → **1 finding, the same false positive** (`paper.py:11`, the literal `ENV_SECRET = "APCA_API_SECRET_KEY"` — an env-var *name*). Independent regex sweep for `APCA_*=`, `PK[A-Z0-9]{16,}`, `secret_key=`, PEM headers → **0**. |
| mutation capability scan | AST over all 26 files in `backend/opaca/`: **0** mutation call sites, **0** attribute references to any forbidden name *anywhere* (the guard now uses string constants rather than attribute access), **0** imports of `requests` / `httpx` / `urllib` / `http` / `aiohttp` / `socket` / `websockets` |
| `python -O` / `-OO` | 303 passed / 1 skipped under both |

The single skip is `test_live_paper_readonly.py`, correctly gated behind `--live-paper`.

---

## RED-TEAM TESTS

**372 passed / 9 findings**, deterministic across repeated runs and identical under `python -O`.

Every one of the 9 "failures" is a deliberate `pytest.fail("FINDING …")` marker placed after its invariant assertions held. `-k "not FINDING"` gives 372 passed. Whole `redteam/` tree against this target: **586 passed / 9 findings** — the 214 treasury-core tests are still green at `d85a2e6`.

### Teeth

The retargeted suite run against the previous commit `3fdabf3`: **88 failed / 293 passed**. The same 381 tests give 372/9 at `d85a2e6`, so **79 tests fail at `3fdabf3` and pass at `d85a2e6`**. Every inverted test has teeth; none of the inversions is vacuous.

`test_teeth.py` independently confirms the concurrency assertions still detect a real breach: with the reservation mechanism monkeypatched out, 60 + 60 concurrent sells reserve 120 shares of a 100-share position and two buys deploy 30,005.62 against 22,000.

### Suite maintenance

Retargeted, not weakened:

* **Inverted (fix proven):** 13 FINDING markers became assertions of the corrected behaviour — 2 in `test_p0c_stale.py`, 4 rewritten as `test_p0d2_replay_gates.py`, 4 in `test_p1b_recon_states.py`, 1 in `test_p1c_seed.py`, 1 in `test_p1ef_audit_approval.py`, 2 in `test_p1g_readonly.py`. Each fails at `3fdabf3`.
* **Brittle assertions replaced with semantic ones:** the persisted policy-row count (`== 11`, which the new `max_snapshot_age_seconds` row broke) now compares the persisted set against `DEFAULT_POLICY_ROWS`; the persisted check count (`== 17`) now compares against `CHECK_ORDER`; the dynamic-dispatch scan's `file:line` allowlist is now a semantic assertion on the call target and enclosing function; the audit-volume cap (`<= 12`) is now "exactly one evaluation event and exactly one terminal decision event per proposal, and no unbounded detail".
* **Nothing weakened:** no adversarial check was deleted or relaxed. 190 → 372 tests.
* **Added:** `test_retest_d85a2e6.py`, 155 tests covering every enumerated retest case plus the named spot-check invariants.

---

## P0-1 REPLAY SAFETY: **CLOSED**

`evaluate_and_reserve` no longer returns from the duplicate-proposal branch before the safety gates. `_snapshot_gate` now runs first for replay and fresh proposals alike, then `_replay_existing` applies the kill switch and approval expiry before handing back a stored decision. `OrchestrationResult.is_auto` is documented as "currently eligible", not "was AUTO once".

| retest case | result |
| --- | --- |
| replay old AUTO under kill switch | blocked, `is_auto=False`, reason `kill switch active`, `RESERVATION_DENIED` audited |
| DRIFT_DETECTED | blocked, reason names `DRIFT_DETECTED` |
| UNKNOWN_REQUIRES_REVIEW | blocked, reason names `UNKNOWN_REQUIRES_REVIEW` |
| stale snapshot (age) | blocked, reason `stale snapshot`, `STALE_SNAPSHOT` audited |
| wrong version | blocked, reason `stale snapshot` |
| missing expected version | blocked, reason `expected_snapshot_version is required` |
| changed proposal / hash | blocked, reason `proposal_id reused with a different payload`, `idempotent_replay=False`, reserved quantity unchanged at 10 |

Idempotency itself survives the fix: a clean replay under current, matching, fresh, reconciled state is still `is_auto=True`, still `idempotent_replay=True`, and still capacity-neutral across five repeats (no extra reservation row, no extra `autonomous_executions` row). A blocked replay adds nothing either — verified across kill-switch and stale-age denials. A blocked replay still reports the stored `authority_result`, so callers do not lose the record. Every gate also denies the replay of an `APPROVAL_REQUIRED` proposal (parametrised over kill switch, drift, stale age, missing version). Replay of a REJECT never becomes AUTO.

**13 tests here fail at `3fdabf3`.**

## P0-2 SNAPSHOT FRESHNESS: **CLOSED**

`expected_snapshot_version` is now mandatory, and `max_snapshot_age_seconds` (default 60) is a policy row read inside the reserving transaction.

| retest case | result |
| --- | --- |
| mandatory expected version | omitted → blocked, `expected_snapshot_version is required`, no proposal row, no reservation |
| 30-day-old snapshot | blocked, `stale snapshot` |
| exact max-age boundary (`now − captured == 60s`) | **accepted** — AUTO |
| just beyond (`+1 microsecond`) | blocked, `stale snapshot`, nothing persisted |
| future snapshot (`captured > now`) | blocked, `snapshot captured_at is in the future`; also on the replay path |
| timezone-aware enforcement | a naive `now` fails closed with nothing persisted; a naive `captured_at` forced into the database fails closed at the codec; a non-UTC aware `now` fails closed; an offset-shifted clock whose wall reading looks recent but whose *instant* is 30 days old is still refused — the gate compares instants, so offset alone cannot forge freshness |

The bound is policy-driven, not hard-coded: setting `max_snapshot_age_seconds = 5` moves the boundary to exactly 5 s / 6 s.

**10 tests here fail at `3fdabf3`.**

## P1-1 READ-ONLY CAPABILITY: **CLOSED**

`AlpacaPaperGateway` no longer retains the client. It captures seven bound read callables under `__slots__`; there is no `_client`, `client`, `_trading_client` or `__dict__` on the instance. `assert_read_only_gateway` now also calls `nested_mutable_client_method`, which rejects a gateway holding a client-like object under any of those three conventional names. `FORBIDDEN_BROKER_MUTATIONS` gained `cancel_order_by_id`, `replace_order_by_id` and the five generic HTTP verbs. The protocol was renamed `ReadOnlyAlpacaGateway`.

Verified against a fake paper `TradingClient` that carries the full mutation surface — **no mutation method was ever invoked**, and no live call was made:

* all 14 names (`submit_order`, `cancel_order`, `cancel_order_by_id`, `cancel_orders`, `replace_order`, `replace_order_by_id`, `close_position`, `close_all_positions`, `exercise_options_position`, `post`, `put`, `patch`, `delete`, `request`) return `None` from `getattr` on the gateway;
* no client attribute is retained, and `__slots__` prevents one being added;
* the guard rejects each of those 14 names planted on the gateway itself, and rejects a nested client under each of the three attribute names;
* the orchestrator turns a gateway with a nested mutable client into `INVALID_BROKER_STATE` rather than proceeding;
* read calls still work and the paper endpoint gate is unchanged (live endpoint, blank endpoint and `_paper=False` are each rejected);
* AST scan: 0 mutation call sites, 0 forbidden-name attribute references, 0 HTTP-client imports anywhere in `backend/opaca/`.

**18 tests here fail at `3fdabf3`.** One residual is recorded below as P1-1-r.

## P1-2 INVALID BROKER STATE: **CLOSED**

`validate_adapted_broker_rows` rejects duplicate position symbols, duplicate `client_order_id`, duplicate broker order ids and `filled > quantity`; `adapt_order_snapshot` checks the quantity pair at the boundary; `compare_state` raises on `quantity_available > quantity` and on malformed order identity; and `reconcile` now wraps the read, the UNKNOWN lookup **and the persistence transaction** in handlers for `InvalidBrokerStateError`, `sqlite3.IntegrityError`, `MoneyError`, `ValueError`, `TypeError`, `ArithmeticError` and `InvalidOperation`.

All 26 corrupt shapes return `INVALID_BROKER_STATE` with no snapshot written and an `INVALID_BROKER_STATE` audit event: duplicate rows, duplicate `client_order_id`, duplicate broker order id, `filled > quantity` (partially-filled and filled), `quantity_available > quantity`, missing/NaN/sNaN/Infinity/negative/text/float/bool/list/huge cash, NaN/text/float/negative position quantity, negative and float order quantity, malformed side, missing and empty `client_order_id`, missing position symbol, short position. **No raw exception escapes the reconciliation boundary in any of them** — asserted separately for every shape, plus a corrupt persisted `unknown_orders` row and a missing asset.

Correctly *not* collapsed into corruption: an unmapped Alpaca status stays `UNKNOWN_REQUIRES_REVIEW` (well-formed payload, unknown meaning), and broker unavailability stays `BROKER_UNAVAILABLE`. A failed reconcile leaves the last good snapshot byte-identical — no half-written replacement.

Malformed prices are caller input rather than broker input, so they are handled at the `PolicyContext` boundary: zero, negative, NaN, Infinity, float, str, `None` and missing all fail closed with no proposal row and no reservation, and `evaluate_and_reserve` now also catches `InvalidBrokerStateError` so a corrupt persisted row becomes a blocked result rather than an escape.

**13 tests here fail at `3fdabf3`.**

## P1-3 APPROVAL EXPIRY: **CLOSED**

`ProposalRecord.is_currently_valid_approval(now)` and `OrchestrationResult.approval_currently_valid(now)` both use `now < expires_at`, and `_replay_existing` refuses an expired approval.

| boundary | result |
| --- | --- |
| 1 s before expiry (`+299 s`) | valid — replay not blocked, `approval_currently_valid` True, `is_auto` still False |
| exactly at expiry (`+300 s`) | **expired** — blocked, reason `approval expired` |
| after expiry (`+301 s`, `+600 s`, `+7 days`) | expired — blocked, `RESERVATION_DENIED` audited with `approval expired` |

Sub-microsecond boundary asserted directly on the record. An expired approval reserves nothing and consumes no autonomous capacity. The helper correctly returns False for AUTO and REJECT results. A hard REJECT still has `expires_at = NULL`, no `approvals` row, and `is_currently_valid_approval` False — human approval cannot resurrect it.

Each boundary case was isolated from the freshness gate by re-reconciling immediately before the replay, so the block reason is the approval, not the snapshot age.

**10 tests here fail at `3fdabf3`.**

## P1-4 QUANTITY_AVAILABLE DRIFT: **CLOSED**

The consistency loop is no longer keyed on local reservations; it now iterates positions and compares the hold-aside against `reserved_by_symbol + broker_sell_remaining`, where a broker order whose `client_order_id` matches a reservation is skipped from the second term.

| retest case | result |
| --- | --- |
| unexplained reduction (100 / 10, nothing local) | `DRIFT_DETECTED`, reason names `unexplained hold-aside` |
| known broker sell explains it (qty 20, filled 5 → 15; hold-aside 15) | `RECONCILED` |
| …but not one share more (hold-aside 16) | `DRIFT_DETECTED` |
| local reservation explains it (reserved 10, hold-aside 10) | `RECONCILED` |
| …but not one share more (hold-aside 11) | `DRIFT_DETECTED` |
| both, distinct identities (10 + 15 = 25; hold-aside 25) | `RECONCILED`; hold-aside 26 → `DRIFT_DETECTED` |
| **both, same identity — no double subtraction** (reservation and the broker order it produced share a `client_order_id`; explained = 10, not 20) | hold-aside 10 → `RECONCILED`; hold-aside 20 → `DRIFT_DETECTED`, proving the same order is not counted twice |
| undeterminable broker sell (no quantity) | `UNKNOWN_REQUIRES_REVIEW`, never `RECONCILED` |
| `quantity_available > quantity` | `INVALID_BROKER_STATE`, no snapshot |
| no hold-aside | `RECONCILED`, no hold-aside reason emitted |

**5 tests here fail at `3fdabf3`.**

## P1-5 SCENARIO SEED TRANSACTION: **CLOSED**

`seed_scenario_once(conn=None)` now re-enters itself under `BEGIN IMMEDIATE`, and an `IntegrityError` on the `scenario_state` insert is re-checked for a concurrent winner before being re-raised.

| retest case | result |
| --- | --- |
| first seed | scenario row + 2 obligations written together |
| repeat (cash 1 / 500,000 / 99,999.99) | identical seed returned; still 2 obligations, still exactly one `SCENARIO_SEEDED` audit event |
| concurrent initialization (4 threads, 4 different cash values) | exactly one scenario row, 2 obligations, one audit event, no errors |
| injected mid-seed failure — the exact `seed-payroll` collision that succeeded at `3fdabf3` | `IntegrityError`, **`get_scenario()` is None**, zero `scenario_state` rows, zero audit events |
| injected failure inside the transaction (audit sink raises) | nothing persisted |
| recovery after a failed attempt | seeds cleanly at the then-current cash |
| reopen + later cash changes (80k / 150k / 500k) | never reseeds; one audit event across all reopens |

**6 tests here fail at `3fdabf3`.**

---

## ATOMIC SELL RESERVATION: **PASS**

60 + 60 against a 100-share position → exactly one AUTO, 60 reserved, loser holds zero reservations and consumes zero autonomous capacity. The full P0-A battery still passes unchanged: 50 + 50, 50 + 50.000000001, 30 × 4, 100 + 100, 1e-9 + 100, same `proposal_id` twice concurrently, the TOCTOU probe holding one worker between `decide()` and `persist_reservations`, two real OS processes, busy/locked database, and rollback after reservation insert. Aggregate active reservations never exceed the reconciled position in any variant.

## ATOMIC CASH RESERVATION: **PASS**

Opening cash 100,000 seeds reserve 40,000 and obligations 38,000, leaving exactly 22,000 deployable. Two concurrent 15,002.81 buys → exactly one AUTO, aggregate deployment ≤ 22,000. The 3-way and 4-way variants, the sequential control, the rolling 24 h notional cap and the runaway hourly order-count cap all still hold under concurrency.

**Other spot-checks:** stale snapshot denial (blocked, nothing persisted); idempotency capacity-neutral over five replays; rollback atomicity (no proposal, no reservation, no authority consumption, and `active_reservations()` empty); reconciliation fail-closed across `BROKER_UNAVAILABLE`, `INVALID_BROKER_STATE`, `DRIFT_DETECTED` and `UNKNOWN_REQUIRES_REVIEW`, each followed by a blocked reservation attempt; Treasury Core regression 230/230.

---

## REMAINING P0

**None.**

## REMAINING P1

**One, new, and narrower than what it replaces.**

**P0-1-r — replay is not re-evaluated against a newer snapshot.** The replay gates verify that the snapshot is current, reconciled, fresh and not future-dated, and that the kill switch is off and the approval unexpired. They do not re-run TreasuryGuard. Reproduced: a BUY that was AUTO at v1 (cash 100,000) still reports `is_auto=True` after a legitimate reconcile to v2 with cash 1 — a valid, fresh, `RECONCILED` snapshot under which an identical *fresh* proposal is a hard REJECT — and the replay reports the stale `snapshot_version=1`. Position changes are caught (they produce drift), but cash, settlement and obligation changes are not.

Not currently exploitable: the reservation already exists, no capacity is added, and no submission path exists. The compensating control is explicit in the module contract — a later approval path must re-reconcile and re-run TreasuryGuard before submission. But `is_auto` is documented as "currently eligible to proceed through the next execution gate", and in this case it is not. Fix shape: on replay, either re-evaluate the decision against the current snapshot, or return `reserved=True` with `is_auto` False whenever `existing.snapshot_version != snapshot.version`.

## REMAINING FAIL-OPEN SAFETY/AUTHORITY ISSUE

**One, latent: P0-1-r above.** It is the only place where a stored authorization verdict outlives the state it was computed against. It cannot produce a trade at this commit because no broker execution path exists, and it must be closed before one is added.

Nothing else is fail-open. Every other divergence found in this retest fails closed.

## P2/P3 BACKLOG

**P3 — P1-1-r, bound read methods still expose their owner.** `AlpacaPaperGateway` holds bound read callables, so `gateway._get_account.__self__` is the `TradingClient` and its mutators remain reachable by introspection from an instance that passes the guard. Confirmed by inspection only — nothing was invoked. Python cannot make the owner truly unreachable while bound methods are held; the cheap hardening is for `assert_read_only_gateway` to walk `__self__` of each retained callable alongside `_client` / `client` / `_trading_client`. Materially narrower than the `_client` attribute it replaces.

**Seven P2 observations carry over unchanged, none fail-open:**

1. **Reservations are never released.** No `UPDATE reservations`, no `DELETE`, no released status, no expiry. Sell capacity and deployable cash are consumed permanently for the life of the database.
2. **One AUTO sell permanently locks out every later BUY of that symbol** — the never-released reservation is surfaced to CHECK-10 as an opposing unresolved order. For a bot meant to trade daily this is the most consequential item on this list, even though it is conservative rather than unsafe.
3. **`proposal_hash` is leg-order sensitive** — it canonicalises JSON keys but not leg order, so a retry that lists the same legs in a different order is permanently unreplayable.
4. **Store mutators outside transactions** — `insert_settlement_event`, `set_kill_switch` and `record_audit` still run on the autocommit connection. `seed_scenario_once` is no longer among them.
5. **A COMMIT failure leaves the connection inside a transaction** — `begin_immediate` rolls back only from its `except` branch. Nothing durable leaks; the next call fails closed with `SqliteBusyError`.
6. **Mutation-scan scope is the package, not the repository** — `spike/spike.py` still calls `submit_order` and `cancel_order_by_id` on a live paper client built from environment credentials. Pre-existing, unchanged from `main`, outside `backend/opaca/`.
7. **The backend test gate is not hermetic** — `tests/helpers.py` resolves `REPO_ROOT/spike/evidence` at import time.

Also noted, not blocking: a corrupt `policies` row (e.g. `per_order_autonomous_notional_max = 0`) still escapes `evaluate_and_reserve` as a raw `MoneyError` rather than a blocked result, and `test_live_paper_readonly.py` is built entirely from bare `assert`s, so under `--live-paper -O` it would pass vacuously.

---

## LIVE PAPER READ-ONLY SMOKE

**NOT RUN.** `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` are absent from this review environment, as they were from the Mac-mini shell. No credentials were requested and no live call was made. `tests/test_live_paper_readonly.py` correctly skips (`live paper smoke not requested`). The repository holds no preserved live-smoke evidence for this phase; the 17 files under `spike/evidence/` are Phase −1B artefacts dated 2026-08-28, unchanged from `main`. **No mutation was performed anywhere in this retest** — the P1-1 probes assert unreachability by inspection and never invoke a mutation method, on a fake client, offline.

---

## MERGE RECOMMENDATION: **MERGE**

All seven findings named for retest are closed, each verified by tests that fail at the previous commit — 79 of them across the suite. No prior P0 or P1 control regressed: the full P0-A concurrency battery, the P0-B cash battery, the 66 malformed-payload probes and the Treasury Core 230 are all still green. Static gates, the credential scan and the mutation capability scan are clean, and everything passes with assertions stripped.

What remains is one narrow latent P1 (P0-1-r), one P3 (P1-1-r) and the seven-item P2 backlog. None can place a trade at this commit, because no broker execution path exists.

Two conditions on the next phase rather than on this merge:

1. **Close P0-1-r before any executor keys on `OrchestrationResult.is_auto`.** It is the last place a historical verdict outlives the state behind it.
2. **Decide the reservation lifecycle (P2-1 / P2-2) before the first fill arrives.** The layer has still never observed reserve → submit → partial fill → re-reconcile → release, and that remains the highest-risk untested assumption in the design.

**Not merged. No production code modified. Nothing committed.**

---

*Artefacts: a detached worktree at `d85a2e6` and one at `3fdabf3` for the teeth comparison, both session-local and outside the repository; the retargeted 381-test Phase 2 suite preserved at `redteam/reconciliation_3fdabf3/` on `review/treasury-red-team`. As before, stale `.lock` entries under `.git/` from earlier sessions could not be pruned — the mount denies unlink.*
