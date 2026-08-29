# Treasury Core — final closeout retest @ bc5fcda

**RED TEAM VERDICT: PASS WITH FINDINGS**

**TARGET SHA:** `bc5fcda36523375c2a5690432713f0843ef451d2`
`git rev-parse origin/feat/treasury-core` → `bc5fcda36523375c2a5690432713f0843ef451d2` — exact match.
Reviewed in an isolated detached worktree at that commit. No production code modified, nothing merged, nothing pushed. Working tree clean at start and end.

Toolchain rebuilt to the pins in `backend/requirements-dev.txt`: Python 3.11.15 · pytest 9.1.1 · ruff 0.16.5 · mypy 2.3.1 (compiled).

---

## GATES

| gate | result |
| --- | --- |
| builder pytest | **230 passed / 0 failed** (202 at 5d33a05; +28, none removed or relaxed) |
| red-team pytest (`review/treasury-red-team` @ 2686192) | **119 passed / 0 failed** |
| independent closeout probes (written fresh for this retest) | **95 passed / 0 failed** |
| ruff check | All checks passed |
| ruff format --check | 33 files already formatted |
| mypy --strict | Success: no issues found in 33 source files |
| git diff --check | clean, exit 0 (worktree vs HEAD, and bc5fcda^..bc5fcda) |
| credential scan | detect-secrets 1.5.0 over 59 tracked files → **0 findings**; regex sweep finds only `os.environ` reads in `spike/spike.py`; 17 evidence JSONs contain no `account_number`, no keys |
| `python -O` / `-OO` | builder 230 passed, red-team 119 passed, probes 95 passed under both |

Targeted reruns (red-team + builder together): leverage isolation 10 · settlement false-liquidity 21 · unresolved SELL reservation 29 · long-only 16 · concentration 56 · partial-fill safety 26 · authority 27 · calendar 51 · fail-closed/money 34. All passed.

**Probe teeth:** the same 95 probes run against `5d33a05` fail **50 / 95**. Every closed finding below is verified by at least one probe that fails at the previous commit and passes here.

---

## THE FIVE RETESTED FINDINGS

| finding | status | evidence |
| --- | --- | --- |
| P1-a `PolicyContext.prices` boundary | **CLOSED** | 21/25 price probes fail at 5d33a05 |
| P1-b `LedgerInconsistencyError` handling | **CLOSED** | 6/9 ledger probes fail at 5d33a05 |
| P2-a future-dated authority history | **CLOSED** | 10/16 authority probes fail at 5d33a05 |
| P2-b calendar input range | **CLOSED** | 9/30 calendar probes fail at 5d33a05 |
| P2-d CHECK-02 bare `assert` / `python -O` | **CLOSED** | AST scan + -O/-OO runs |

### PRICE BOUNDARY — CLOSED

`PolicyContext.__post_init__` now runs every price through `require_positive_decimal` (must already be a `Decimal`, then `positive_money`) and re-seals the mapping as a `MappingProxyType`.

Independently reproduced at bc5fcda:

* the 5d33a05 exploit — zeroing `prices["SGOV"]` so a 100,000-notional single-symbol buy passes CHECK-04 and reaches **AUTO** — is now unreachable: the context cannot be constructed.
* the vacuous-CHECK-04 path (held 95k position, price −100, pool base ≤ 0 → "vacuous" PASS) is likewise unreachable.
* 13 malformed inputs all raise `MoneyError`, never `TypeError`: float, bool, `None`, str, int, NaN, sNaN, ±Infinity, `1e26` (at the magnitude limit), `1e30`, `-0`, `0.00`. Strings and ints are **not** coerced.
* a bad price on an untraded symbol is rejected too — validation is whole-mapping.
* the sealed mapping cannot be written to, and mutating the caller's dict after construction does not change the context; `dataclasses.replace` re-validates.
* teeth retained: with an honest price the 100k concentration still FAILS CHECK-04 and never reaches AUTO; a *missing* price still fails closed in CHECK-04 **and** CHECK-11.

Design note: an invalid price is now rejected at construction rather than converted into a failed check. No decision is produced, so it is fail-closed — but it is a different shape from `MissingPriceError`/`CalendarError`, which become failed checks. Callers must treat `PolicyContext(...)` as a validating boundary that raises. Acceptable; worth one line in the adapter runbook.

### LEDGER INCONSISTENCY — CLOSED

`_build_frame` now catches `LedgerInconsistencyError`, carries `liquidity=None` plus a `ledger_error` string, and every liquidity-dependent check returns a hard failure with the ledger detail. All six `frame.liquidity` accesses in `engine.py` are guarded (CHECK-01, -02, -04, -06, -11, -12) — verified by grep and by probe.

Reproduced: broker cash 100,000 against 200,000 of unsettled proceeds now returns a `PolicyDecision` with CHECK-01/02/04/06/11 failed and `ledger inconsistent … (fail closed)`; `decide()` returns REJECT. A **sell-only** proposal fails closed on CHECK-12 rather than passing vacuously. An empty proposal fails closed. The domain-level guard is unchanged (`compute_liquidity` still raises). Boundary is exact: 100,000 vs 100,000 unsettled is consistent; 100,000.01 is not. A consistent ledger shows no ledger reason anywhere.

### FUTURE HISTORY — CLOSED

`executions_in_window` is now `timestamp > now - window` with no upper bound. Reproduced: identical 49,000 of history plus a 2,000 proposal now gives APPROVAL_REQUIRED whether stamped one second in the past **or** one second in the future (it gave AUTO in the future case at 5d33a05). Forward skew of 1s, 5m, 23h, 25h and 1 year all still count. The 24h cutoff is unchanged — exactly `window` old is still excluded, one second inside is included — and stale history still expires to AUTO, so the fix is not vacuous. Future-dated entries also count toward the rolling order count and toward the CHECK-13 runaway hourly limit. Monotonicity probe: moving a stamp forward can only increase in-window membership, never decrease it.

### CALENDAR INPUT RANGE — CLOSED

`USTradingCalendar` now overrides `next_trading_day`, `add_trading_days` and `settlement_date` to `_require_supported(input)` before delegating. `settlement_date(2024-12-31)` raises `CalendarError` instead of returning 2025-01-02. All three APIs reject 7 out-of-range inputs each, including `date.min`/`date.max` (which produced an overflow rather than `CalendarError` at 5d33a05). `add_trading_days(day, 0)` — which short-circuits the walk, so only an input guard can catch it — now validates. `trading_days_between` fails closed on out-of-range bounds via its candidate walk. In-range behaviour unchanged: range endpoints accepted, T+1 over Labor Day weekend still 2026-09-04 → 2026-09-08, end-of-range exhaustion still fails closed, `cycle=0` still rejected.

### SAFETY ASSERT — CLOSED

`assert worst_value is not None` is gone, replaced by `min(ranked)` over `(available, day)` pairs with an explicit fail-closed branch. AST scan finds **zero** `assert` statements anywhere in `backend/opaca/`. Builder (230), red-team (119) and probes (95) all pass under `python -O` and `-OO`. Tie-break semantics preserved: `min` over `(value, date)` picks the earliest day among equal values, matching the old strict-`<` loop over `sorted(dates)`. CHECK-02 still fails when the operating reserve is breached.

---

## REGRESSION IN PRIOR P0/P1 CONTROLS

**NONE.** RT-01…RT-10, NEW-01 and NEW-03 all still green (119/119). No test was deleted, weakened or relaxed between 5d33a05 and bc5fcda — the only removed lines in `backend/tests/` are two import statements that were rewritten. Test count went 202 → 230.

---

## NEW FINDINGS AT bc5fcda

All three are **P3**, none is a regression, and none is fail-open.

**P3-a — notional overflow still escapes `evaluate()`.** `ProposedOrder` validates `quantity` and `reference_price` individually, but `notional` is a lazily-computed property `round_budget(quantity * reference_price)`. Quantity `1e13` and price `1e13` each pass their own validator; their product hits `MAGNITUDE_LIMIT` and `MoneyError` escapes `evaluate()` rather than becoming a failed check. The same shape exists inside CHECK-04, which multiplies `existing_quantity * reference_price` itself. Present identically at 5d33a05 — pre-existing, not introduced here. Fail-closed (no decision, never AUTO) and unreachable from plausible broker data. Fix alongside the price boundary: validate the product at the `ProposedOrder`/frame boundary.

**P3-b — `assess_partial_fill_safety` still calls `compute_liquidity` unguarded.** `_SellCoverage.__init__` raises a raw `LedgerInconsistencyError` on an inconsistent ledger. Unreachable through `decide()`, which short-circuits on the failed base decision (verified: `decide()` returns REJECT), so this is defence-in-depth only. But it is the one remaining caller of `compute_liquidity` outside the guarded frame, and any future path that assesses partial-fill safety before or independently of the base evaluation would get the exception back.

**P3-c — construction-time rejection vs failed check.** Noted under PRICE BOUNDARY above: invalid prices raise at `PolicyContext` construction while missing prices become failed checks. Consistent enough to be safe, inconsistent enough to document.

---

## SUMMARY

**REMAINING P0:** none.

**REMAINING P1:** none.

**REMAINING P2 (backlog, all carried over, none retested-and-failed):**

1. Eligible-vs-all scoping mismatch in the CHECK-04 zero-pool fail-closed branch (P2-c at 5d33a05, unchanged — `"investment pool base is zero but projected holdings exist; fail closed"` still present). Availability-only, fails closed.
2. NEW-02 (accepted by design): an UNKNOWN same-logical SELL blocks its own idempotent retry. Recovery requires reconciliation first; the runbook must say so.
3. Test gate is not hermetic — `backend/tests/helpers.py` still resolves `REPO_ROOT/spike/evidence`, so a backend-only checkout fails spuriously.

**REMAINING FAIL-OPEN AUTHORITY OR SAFETY ISSUE:**

None at the policy-engine boundary. One structural gap is unchanged and is not a code defect at this commit:

**The atomic reservation layer still does not exist.** Two evaluations against the same snapshot both pass CHECK-16 and both return `passed=True`, jointly selling 120 shares of a 100-share position. The stateless engine cannot close this; `opaca/policy/engine.py`'s module docstring correctly requires an ATOMIC single-writer SQLite transaction before broker submission. Every RT-01 guarantee remains conditional on that layer being built correctly. **No broker execution path may be added before it exists.**

Second, unchanged: no reconciliation/adapter layer exists, so the engine has never been tested against real broker state. The P1-a fix materially reduces the exposure — the engine now defends its own price boundary rather than trusting the caller — but the assumption that everything handed to `PolicyContext` is reconciled remains the highest-risk untested assumption.

**MERGE RECOMMENDATION: MERGE**

Both P1s from the previous retest are closed, verified against independently written probes that fail at the previous commit. All prior P0/P1 remediation holds. Static gates and the credential scan are clean, and everything passes with assertions stripped. What remains is three P3s and three P2 backlog items, none of which is fail-open and none of which blocks a merge of a non-executing policy core.

**Not merged. Not pushed. Nothing changed in the repository.**

---

*Retest artefacts: worktree at `bc5fcda`, comparison worktree at `5d33a05`, and the 95-test independent probe suite now preserved on this branch at `redteam/closeout_bc5fcda/`.*
