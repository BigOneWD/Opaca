# OPACA Phase 2 — final closeout retest @ 624439f

**RED TEAM VERDICT: PASS**

**TARGET SHA:** `624439fbba9a2f70110e4c413a7783eda564418a`

*Note on the target.* The brief carried the literal placeholder `<NEW_SHA>`. It was resolved from the repository, and the resolution is unambiguous: `origin/feat/reconciliation-state` has advanced by exactly one commit above the stated previous SHA — `624439f`, *"fix: separate replay history from execution eligibility"*, whose parent is `d85a2e62b5d4c3852dcd5322eb4d2c907fbec32e` and whose subject matches the one issue named for retest. All results below are against that commit.

Reviewed in an isolated detached worktree. No production code modified, nothing merged, no broker execution added, the remaining replay issue not fixed by me, nothing committed. Worktree clean at start and end. Toolchain unchanged: CPython 3.11.15 · pytest 9.1.1 · ruff 0.16.5 · mypy 2.3.1 (compiled).

The diff is 4 files, +327 / −13: `backend/opaca/orchestration/reserve.py` (+33/−6, the only production change), `backend/tests/test_atomic_reservation.py` (+221), `docs/reconciliation-state.md`, and a new `docs/backlog.md`.

---

## REPLAY CURRENT-ELIGIBILITY: **CLOSED**

The fix is one guard clause in `OrchestrationResult.is_auto`:

```python
if self.idempotent_replay:
    return False
```

plus contract documentation in `reserve.py`, `_replay_existing` and `docs/reconciliation-state.md`. A replayed proposal still returns `authority_result=AUTO`, `reserved=True`, `blocked=False` and `idempotent_replay=True` — the history and the reservation identity survive — but it no longer claims current execution eligibility.

## HISTORICAL AUTO ≠ CURRENT AUTO: **PROVEN**

Proven three ways, not merely asserted once:

1. **Structurally.** Across fresh AUTO, clean replay, kill-switched replay, stale replay and version-less replay, `is_auto and idempotent_replay` is never simultaneously true.
2. **Behaviourally, per case.** All six enumerated retest cases below.
3. **By agreement with a fresh sibling.** The exact P0-1-r reproduction now converges: at the collapsed-cash snapshot, `replay.is_auto == fresh.is_auto == False`, where before the replay said True and the fresh sibling said REJECT.

### 1 — Historical AUTO replay with no state change

Replayed five times against the identical snapshot: `idempotent_replay=True`, `blocked=False`, `block_reason=None`, `authority_result=AUTO`, `reserved=True`, same `proposal_hash` — and **`is_auto=False` every time**.

* **Idempotent:** yes. The reservation rows are byte-identical before and after five replays (`reservation_id`, kind, symbol, quantity compared row by row), and there are exactly 2 of them — one `SELL_QUANTITY`, one `ORDER_IDENTITY`.
* **No duplicate reservation:** yes — verified as row identity, not just as a count.
* **No duplicate authority consumption:** the `autonomous_executions` list is identical by `(timestamp, notional)` after five replays. `proposal_legs`, `order_identity` and `authority_decisions` each stay at exactly 1 row.
* **Replay does not assert current executable AUTO:** confirmed.
* Audit is correct too: one `PROPOSAL_EVALUATED`, one `RESERVATION_CREATED`, three `IDEMPOTENT_REPLAY` for three replays.
* The fix does not break fresh evaluation: a fresh proposal still reaches `is_auto=True`.

### 2 — Historical AUTO after cash 100,000 → 1

Reconcile to cash 1 is legitimately `RECONCILED` (positions unchanged), so this is the hardest case — no gate fires.

* replay `is_auto=False`, `idempotent_replay=True`
* a fresh sibling proposal at the same snapshot is a hard **REJECT**, reserves nothing
* replay and sibling now agree

### 3 — Historical AUTO after a new obligation

A 55,000 obligation inserted after the AUTO, then re-reconciled at unchanged cash: replay is not executable, and the fresh sibling hard-REJECTs.

### 4 — Historical AUTO after reserve / policy change

* operating reserve raised 40,000 → 61,000: replay not executable, fresh sibling not executable
* policy tightened, parametrised over six rows — `per_order_autonomous_notional_max`, `per_proposal_aggregate_notional_max`, `rolling_24h_autonomous_notional_max`, `concentration_max_fraction`, `min_trade_notional`, `permitted_symbols`: replay not executable in every case
* a tightened whitelist hard-REJECTs a fresh sibling

### 5 — All gates still fail closed

| gate | replay result |
| --- | --- |
| kill switch | `blocked`, `is_auto=False`, reason `kill switch active` |
| DRIFT_DETECTED | `blocked`, reason names `DRIFT_DETECTED` |
| UNKNOWN_REQUIRES_REVIEW | `blocked`, reason names `UNKNOWN_REQUIRES_REVIEW` |
| stale snapshot (age) | `blocked`, reason `stale snapshot` |
| version mismatch | `blocked`, reason `stale snapshot` |
| missing expected version | `blocked`, reason `expected_snapshot_version is required` |

No gate adds capacity: reservation count, authority history, aggregate reserved quantity and aggregate reserved cash are all unchanged after a kill-switched replay, a stale replay and a version-less replay in sequence.

### 6 — Proposal identity

* exact retry ×4: idempotent, same hash, `reserved=True`, `blocked=False`, capacity unchanged
* changed content under the same `proposal_id`: `blocked`, `is_auto=False`, `idempotent_replay=False`, reason `proposal_id reused with a different payload`, different hash, capacity unchanged, `RESERVATION_DENIED` audited
* parametrised over five mutations — quantity, price, side, symbol, added leg — all fail closed with capacity unchanged
* a different `proposal_id` is still a fresh evaluation and still reaches AUTO

**Approval path:** replaying an `APPROVAL_REQUIRED` proposal is likewise not `is_auto`, while `approval_currently_valid` still correctly reports an unexpired approval.

---

## IDEMPOTENCY: **PASS** · NO DUPLICATE RESERVATION: **PASS**

Beyond the six cases: the pre-existing idempotency battery still passes unchanged — exact retry after commit, retry after rollback, retry while the first transaction is open (threads), same `proposal_id` twice concurrently across separate connections, duplicate reservation insert refused by `idx_reservations_active_sell`, `client_order_id` collision failing closed atomically, and replay of a REJECT staying rejected.

## ATOMIC SELL: **PASS** · ATOMIC CASH: **PASS**

60 + 60 against 100 → exactly one AUTO, 60 reserved, loser holds nothing and consumes no authority. The full P0-A battery (50+50, 50+50.000000001, 30×4, 100+100, 1e-9+100, same-id concurrent, the TOCTOU hold between `decide()` and `persist_reservations`, two real OS processes, busy database, rollback after insert) is unchanged and green. Cash: 15,002.81 × 2 against exactly 22,000 deployable → one AUTO, aggregate ≤ 22,000; 3-way and 4-way variants, the sequential control, the rolling 24 h notional cap and the runaway hourly count cap all hold.

## REGRESSION SPOT-CHECK

| area | result |
| --- | --- |
| atomic sell reservation | PASS |
| atomic cash reservation | PASS |
| snapshot freshness (mandatory version, 30-day, exact 60 s boundary, +1 µs, future, tz) | PASS |
| reconciliation fail-closed (all five states, 66 malformed payloads) | PASS |
| read-only Alpaca capability (14 mutator names, nested-client guard, AST scan) | PASS |
| invalid broker-state handling (26 corrupt shapes, no raw escape) | PASS |
| approval expiry (before / exactly at / after) | PASS |
| transactional seed-once (first, repeat, concurrent, mid-seed failure, reopen, later cash) | PASS |
| Treasury Core | 230 passed, unchanged |

---

## BUILDER TESTS

| gate | result |
| --- | --- |
| full builder suite | **309 passed / 1 skipped** (303 at `d85a2e6`; +6, none removed) |
| Treasury Core regression (14 files) | **230 passed** — unchanged |
| Phase 2 files (7 files) | **79 passed / 1 skipped** |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 58 files already formatted |
| `mypy --strict` | Success: no issues found in 58 source files |
| `git diff --check` | clean, exit 0 (worktree, and `d85a2e6..624439f`) |
| credential scan | detect-secrets 1.5.0 over 86 tracked files → **1 finding, the same false positive** (`paper.py:11`, the literal env-var *name*). Regex sweep → 0. |
| mutation scan | AST over all 33 files in `backend/opaca/`: **0** mutation call sites, **0** forbidden-name attribute references, **0** HTTP/socket imports, **0** bare `assert` |
| `python -O` / `-OO` | 309 passed / 1 skipped under both |

The builder's own test churn is clean: six new replay tests added, **no test function removed**, and the only deleted assertions are the two `is_auto is True` replay lines that encoded the behaviour being fixed.

## RED-TEAM TESTS

**409 passed / 8 findings** (417 collected), deterministic across three consecutive runs. `-k "not FINDING"` → 409 passed. Whole `redteam/` tree: **623 passed / 8** — the 214 treasury-core tests are still green.

**Teeth.** The same suite against the previous commit `d85a2e6`: **23 failed / 394 passed**. Fifteen of those failures are the tests that close this fix; the other eight are the carried markers, which fail at both. Against the original baseline `3fdabf3`: **109 failed / 308 passed**.

**Suite maintenance — minimal and additive.** Exactly five files changed, and `git status` confirms nothing outside `redteam/`:

* `test_retest_624439f.py` — new, 36 tests across the six enumerated cases.
* `test_p0d2_replay_gates.py` — the P0-1-r FINDING marker inverted to assert the corrected behaviour (it now proves replay and a fresh sibling agree); and `test_a_clean_replay_is_still_auto_and_capacity_neutral` retargeted to `..._is_idempotent_and_never_currently_auto`.
* `test_retest_d85a2e6.py` — the same clean-replay contract retargeted.
* two READMEs and the `conftest.py` docstring.

The two retargeted tests **assert more than they did before**, not less: they kept idempotency and capacity neutrality and added preserved history (`authority_result`, `reserved`), `blocked is False`, and the absence of current eligibility. They were the only two places in the suite that encoded the old contract. **No unrelated adversarial test was touched.**

---

## REMAINING P0

**None.**

## REMAINING P1

**None.**

## REMAINING FAIL-OPEN SAFETY/AUTHORITY ISSUE

**None.** Every divergence still open fails closed. This is the first pass in this review where that sentence has no qualifier attached to it.

One contract caveat worth carrying into the execution phase, not a defect: on replay, `reserved` is still `True` while `is_auto` is `False`. An executor must key on `is_auto` (or on a fresh evaluation), never on `reserved` alone. The docs now say this explicitly, and `is_auto` has no consumer inside the package — it is a caller-facing property only.

## P2/P3 BACKLOG

The builder has recorded the backlog in `docs/backlog.md`, which matches this review's findings closely enough to serve as the handover artefact. Independently re-verified as still open, none fail-open:

* **P3 — bound-method `__self__` introspection escape.** `AlpacaPaperGateway` retains bound read callables, so `gateway._get_account.__self__` is the `TradingClient` and its mutators remain reachable by introspection from an instance that passes the guard. Inspected only; nothing invoked. The cheap hardening is for `assert_read_only_gateway` to walk `__self__` of each retained callable alongside `_client` / `client` / `_trading_client`.
* **P2 — reservations are never released.** No release, expiry or reconciliation-driven cleanup exists.
* **P2 — one AUTO sell permanently locks out later buys of that symbol** via CHECK-10. The most operationally consequential item for a bot meant to trade daily, and a direct consequence of the item above.
* **P2 — `proposal_hash` is leg-order sensitive.**
* **P2 — three store mutators run outside transactions** (`insert_settlement_event`, `set_kill_switch`, `record_audit`).
* **P2 — a COMMIT failure leaves the connection inside a transaction.** Nothing durable leaks; the next call fails closed.
* **P2 — the mutation scan covers `backend/opaca` only**; `spike/spike.py` still calls `submit_order` and `cancel_order_by_id` on a live paper client.
* **P2 — the test gate is not hermetic.** One wording note: `docs/backlog.md` describes this as the risk of a CI miss running live tests, whereas the finding is narrower and concrete — `backend/tests/helpers.py` resolves `REPO_ROOT/spike/evidence` at import time, so a backend-only checkout fails collection. Worth aligning the entry.

Also carried, unlisted in the backlog: a corrupt `policies` row still escapes `evaluate_and_reserve` as a raw `MoneyError` rather than a blocked result, and `test_live_paper_readonly.py` is built entirely from bare `assert`s, so under `--live-paper -O` it would pass vacuously.

---

## LIVE PAPER READ-ONLY SMOKE

**NOT RUN.** `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` are absent from this environment. No credentials were requested and no live call was made; `tests/test_live_paper_readonly.py` correctly skips. **No mutation was performed anywhere in this retest** — the read-only probes assert unreachability by inspection against a fake client, offline.

---

## MERGE RECOMMENDATION: **MERGE**

The last fail-open in the layer is closed, by the smallest change that could close it, verified by 15 tests that fail at the previous commit. Nothing regressed: the builder suite grew 303 → 309 with no test removed, Treasury Core is untouched at 230, and the 409-test red-team suite is green apart from eight deliberate markers for a documented backlog. Static gates, the credential scan and the mutation capability scan are clean, and everything passes with assertions stripped.

Two things belong in the execution phase's charter rather than in this merge:

1. **The reservation lifecycle (P2-1 / P2-2).** The layer has still never observed reserve → submit → partial fill → re-reconcile → release. That remains the highest-risk untested assumption in the design, and it is where the next phase should start.
2. **The pre-execution sequence is now written down** in `docs/reconciliation-state.md` — fresh reconciliation → latest snapshot → TreasuryGuard re-run → authority re-run → reservation revalidation → only then eligibility. It should be implemented as code, not honoured as convention.

**Not merged. No production code modified. Nothing committed.**

---

*Artefacts: detached worktrees at `624439f`, `d85a2e6` and `3fdabf3` (session-local, outside the repository) and the 417-test Phase 2 suite in `redteam/reconciliation_3fdabf3/` on `review/treasury-red-team`, currently uncommitted. Stale `.lock` entries under `.git/` from earlier sessions still cannot be pruned — the mount denies unlink.*
