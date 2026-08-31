# Opaca — red-team suite

Five rounds of adversarial review live here:

* `redteam/*.py` + `closeout_bc5fcda/` — **Treasury Core** (`feat/treasury-core`)
* `reconciliation_3fdabf3/` — **Phase 2 reconciliation state + SQLite atomic
  reservation** (`feat/reconciliation-state`)
* `paper_execution_79a7b1b/` — **Phase 3 paper execution lifecycle**
  (`feat/paper-execution`)
* `prelive_11d1cde/` — **Pre-live readiness: bounded live-paper pricing and
  read-only preflight** (`feat/prelive-readiness`)
* `closeout_193d7a2/` — **Final pre-live closeout: the last four blockers**
  (`feat/prelive-readiness`)

Each subdirectory carries its own README, and every suite runs against a
checkout of the commit it reviews, never against this branch.

---

## Treasury Core — red-team suite

Adversarial tests written to **falsify** the builder's report for

    origin/feat/treasury-core @ 5d33a052655324e607b808e8239b78befe94be18

These tests are red-team-only and live on `review/treasury-red-team`.
They are never added to the builder branch and they do not import from this
branch — they run against a checkout of the builder commit:

    git worktree add --detach /tmp/tc 5d33a052655324e607b808e8239b78befe94be18
    OPACA_BACKEND=/tmp/tc/backend pytest -q redteam/

| file | attack class |
| --- | --- |
| `test_p0_a_leverage.py` | P0-A cash / leverage isolation |
| `test_p0_b_settlement.py` | P0-B settlement false liquidity, double counting |
| `test_p0_c_longonly.py` | P0-C long-only / oversell, cross-proposal |
| `test_p0_d_authority.py` | P0-D authority splitting, rolling-window boundaries |
| `test_p1_b_partialfill.py` | P1-B partial-fill subset enumeration |
| `test_p1_c_calendar.py` | P1-C calendar / settlement / blackout |
| `test_p1_de_failclosed_money.py` | P1-D fail-closed, P1-E money / Decimal |
| `test_p2_interpretations.py` | P2 spec-interpretation consequences |
| `test_rt_fixes.py` | attacks on the RT-01..RT-10 remediation itself |
| `closeout_bc5fcda/` | independent closeout probes for the final retest (95) |

## Status against bc5fcda (final closeout)

Target: `origin/feat/treasury-core @ bc5fcda36523375c2a5690432713f0843ef451d2`
(`fix: harden treasury core input boundaries`).

    git worktree add --detach /tmp/tc bc5fcda36523375c2a5690432713f0843ef451d2
    OPACA_BACKEND=/tmp/tc/backend pytest -q redteam/     # -> 214 passed

**214 passed / 0 failed** — the 119 original adversarial tests plus the 95
independent closeout probes in `closeout_bc5fcda/`. Verdict:
**PASS WITH FINDINGS**, merge recommended.

The five findings named for retest are all CLOSED and each is verified by
probes that fail at the previous commit `5d33a05`:

| finding | probes failing at 5d33a05 |
| --- | --- |
| P1-a `PolicyContext.prices` boundary | 21 / 25 |
| P1-b `LedgerInconsistencyError` handling | 6 / 9 |
| P2-a future-dated authority history | 10 / 16 |
| P2-b calendar input range | 9 / 30 |
| P2-d CHECK-02 bare `assert` / `python -O` | 2 / 5 |

50 of the 95 probes fail at `5d33a05` and all 95 pass at `bc5fcda`. No prior
P0/P1 control regressed; nothing in this suite was weakened for the retest.

Three P3 residuals stay OPEN and are pinned as characterisation tests in
`closeout_bc5fcda/test_residual_escape_probe.py`. They are unchanged from
`5d33a05` — not regressions. See `closeout_bc5fcda/README.md` and the full
report at `claude/treasury-core-redteam-closeout-bc5fcda.md`.

## Status against 5d33a05

**119 passed / 0 failed.** The suite is GREEN against correct behaviour.

The 15 tests that characterised RT-01..RT-10 at 2c5a6d8 were rewritten at
d06f8ea to assert the corrected behaviour. At 5d33a05 the two remaining OPEN
characterisation tests, NEW-01 and NEW-03, have likewise been inverted to
assert the corrected behaviour:

* `test_NEW01_non_permitted_holding_gives_no_denominator_and_no_headroom`
  — a non-whitelisted holding must not enlarge the investment pool base, must
  not buy concentration headroom, and must not be an offender itself.
* `test_NEW03_monotonic_de_risking_is_allowed_from_a_pre_existing_breach`
  — from a pre-existing breach the projection may stay above the limit while
  every pre-existing offender strictly improves and no compliant symbol
  becomes a new offender (95% -> 85% at a 70% limit PASSES). Non-improving,
  net-zero, worsening and new-offender cases still FAIL.

Both inverted tests have teeth: run against d06f8ea they fail 2/2
(NEW-01 measures 140,000 of headroom instead of 70,000; NEW-03's 95% -> 85%
sell is rejected).

    # proves the assertions have teeth
    OPACA_BACKEND=<worktree-at-d06f8ea>/backend pytest -q redteam/test_rt_fixes.py \
        -k "NEW01 or NEW03"                  # -> 2 failed

NEW-02 is unchanged and still passes: an UNKNOWN same-logical SELL remains
reserved / fail-closed. It stays a characterisation test of a deliberate
conservative choice, not a defect.

---

## Phase 2 — reconciliation state @ 3fdabf3

Target: `origin/feat/reconciliation-state @ 3fdabf3bcbe3e0d8c8ccfb5a9feedad584c4b6e2`
(`test: add reconciliation failure, concurrency guards, and docs`), on base
`main @ b940c719` (tag `treasury-core-complete`).

    git worktree add --detach /tmp/rc 3fdabf3bcbe3e0d8c8ccfb5a9feedad584c4b6e2
    OPACA_BACKEND=/tmp/rc/backend pytest -q redteam/reconciliation_3fdabf3
    #   -> 190 passed, 21 failed
    OPACA_BACKEND=/tmp/rc/backend pytest -q redteam/reconciliation_3fdabf3 -k "not FINDING"
    #   -> 190 passed

**190 passed / 21 findings.** Verdict: **PASS WITH FINDINGS**,
**FIX THEN RETEST** — not merged.

Every one of the 21 failures is a deliberate `pytest.fail("FINDING …")` marker
placed after its invariant assertions held; the message is the finding. Both
figures are deterministic across repeated runs and identical under `python -O`.

Builder gates all reproduce at the target: 273 passed / 1 skipped overall
(230 Treasury Core regression, 43 new + 1 live-paper skipped), ruff clean,
`ruff format --check` clean on 58 files, `mypy --strict` clean on 58 files,
`git diff --check` exit 0, 0 bare `assert` in `backend/opaca/`, and a credential
scan whose single hit is the literal env-var *name* `"APCA_API_SECRET_KEY"`.

Teeth: with the reservation mechanism monkeypatched out, two concurrent
`SELL 60` proposals reserve 120 shares of a 100-share position and two buys
deploy 30,005.62 against 22,000 of deployable cash (`test_teeth.py`). The green
concurrency results are detections, not vacuous passes.

| area | verdict |
| --- | --- |
| atomic sell reservation | PASS |
| atomic cash reservation | PASS |
| stale snapshot | FAIL |
| idempotency (capacity) | PASS |
| scenario seed-once | PASS |
| SQLite durability | PASS |
| read-only Alpaca guarantee | FAIL |
| reconciliation fail-closed | FAIL (replay path only) |

Three items are named as must-fix before merge, each because the execution
phase will build on it:

1. **P0-1** — the idempotent-replay branch returns `is_auto=True` with the
   reconciliation-status, stale-snapshot and kill-switch gates all skipped.
2. **P0-2** — no maximum snapshot age, and `expected_snapshot_version` is
   optional, so the staleness guard is caller-supplied.
3. **P1-1** — the read-only gateway retains a mutable `TradingClient` on
   `_client`, and the mutation blacklist misses `cancel_order_by_id` /
   `replace_order_by_id`.

No broker execution path may be added until P0-1 and P0-2 are closed. Nothing
found here can place a trade at this commit: no submission path exists.

Cross-check — the whole tree against the Phase 2 target:

    OPACA_BACKEND=/tmp/rc/backend pytest -q redteam/
    #   -> 404 passed, 21 failed   (214 treasury-core + 190 Phase 2 invariants,
    #                               plus the 21 Phase 2 FINDING markers)

The 214 treasury-core tests are unchanged and still green at `3fdabf3`, which
confirms Phase 2 touched no Treasury Core behaviour.

Full report: `claude/reconciliation-state-redteam-3fdabf3.md`.

### Remediation retest @ d85a2e6

Target: `origin/feat/reconciliation-state @ d85a2e62b5d4c3852dcd5322eb4d2c907fbec32e`
(`fix: close reconciliation state red-team findings`).

    git worktree add --detach /tmp/rc d85a2e62b5d4c3852dcd5322eb4d2c907fbec32e
    OPACA_BACKEND=/tmp/rc/backend pytest -q redteam/reconciliation_3fdabf3
    #   -> 372 passed, 9 failed
    OPACA_BACKEND=/tmp/rc/backend pytest -q redteam/
    #   -> 586 passed, 9 failed   (214 treasury-core still green)

**372 passed / 9 findings.** Verdict: **PASS WITH FINDINGS**, merge recommended.

All seven findings named for retest are CLOSED. The suite was retargeted: the
inverted tests assert the corrected behaviour and fail 86/86 against `3fdabf3`,
so none of them is vacuous. Brittle assertions were replaced with semantic ones
(the policy-row and check-row counts now compare against
`DEFAULT_POLICY_ROWS` / `CHECK_ORDER`; the dynamic-dispatch scan asserts on the
call target and enclosing function instead of a `file:line` allowlist). No
adversarial check was weakened.

Two residuals were raised by the retest (P0-1-r replay is not re-evaluated
against a newer snapshot; P1-1-r bound read methods still expose their owner),
and seven P2 observations carry over unchanged.

Full report: `claude/reconciliation-state-retest-d85a2e6.md`.

### Final closeout retest @ 624439f

Target: `origin/feat/reconciliation-state @ 624439fbba9a2f70110e4c413a7783eda564418a`
(`fix: separate replay history from execution eligibility`).

    git worktree add --detach /tmp/rc 624439fbba9a2f70110e4c413a7783eda564418a
    OPACA_BACKEND=/tmp/rc/backend pytest -q redteam/reconciliation_3fdabf3
    #   -> 409 passed, 8 failed
    OPACA_BACKEND=/tmp/rc/backend pytest -q redteam/
    #   -> 623 passed, 8 failed   (214 treasury-core still green)

**409 passed / 8 findings.** Verdict: **PASS**, merge recommended.

The last remaining fail-open, P0-1-r, is closed: `OrchestrationResult.is_auto`
returns False whenever `idempotent_replay` is True, so a replayed proposal never
asserts current execution eligibility. Replay still preserves the historical
authority result and reservation rows and still consumes no new capacity.

`test_retest_624439f.py` adds 36 tests across the six enumerated retest cases;
15 tests fail at `d85a2e6` and pass here. Two tests that encoded the old replay
contract (`replay.is_auto is True`) were retargeted to the new one and now
assert more than they did before — idempotency, capacity neutrality, preserved
history *and* the absence of current eligibility. No unrelated adversarial test
was touched.

Remaining: one P3 (`__self__` introspection escape) and seven P2 observations,
all fail-closed and all now recorded by the builder in `docs/backlog.md`.

Full report: `claude/reconciliation-state-closeout-624439f.md`.

---

## Phase 3 — paper execution @ 79a7b1b

Target: `origin/feat/paper-execution @ 79a7b1b837c86dc700533eeda6f5699b197a7d4a`
(`feat: implement safe Alpaca paper execution lifecycle`), on production baseline
`main @ 12f4eb02f9c832f7368cc0c06f67afaf4bb1d7d8`.

    git worktree add --detach /tmp/pe cd3dc86b7153718bbc98072a79a81ae3587f9477
    OPACA_BACKEND=/tmp/pe/backend pytest -q redteam/paper_execution_79a7b1b
    #   -> 139 passed, 4 failed   (all four are PRE-LIVE readiness reports)
    OPACA_BACKEND=/tmp/pe/backend pytest -q redteam/
    #   -> 765 passed, 10 failed

**Architecture review @ 79a7b1b:** 94 passed / 7 findings — PASS WITH FINDINGS,
FIX THEN RETEST. **Final retest @ cd3dc86: 135 passed / 0 execution-safety
findings** — all five closed, verdict **PASS WITH FINDINGS** (the remaining four
markers are pre-live readiness reports, not defects), **MERGE** recommended.

This is the first phase that can place an order. Reviewed **offline only** — no
credentials requested, no live call, and **no mutation of any kind performed**,
including against the paper endpoint.

The mutation surface is two call sites, both in `execution/service.py`, both on
the injected `mutate_gateway`. No attack produced a duplicate order, an oversell,
an over-release, a submission under a closed gate, or a live-endpoint call.

Two Phase 2 findings are **closed** by this phase: reservations are now released
against proven disposition, and the permanent opposing-buy lockout is gone.

Five findings were raised at `79a7b1b`, all fail-closed, and **all are CLOSED at
`cd3dc86`** — each verified by tests that fail at the previous commit (30 in total):

| id | summary | closed by |
| --- | --- | --- |
| P1-1 | `compare_state` skipped the explained-delta comparison at a zero raw delta | `delta == expected` for every symbol, including symbols only in the explained set |
| P1-2 | a live order counted twice against its own capacity | broker orders with a locally known `client_order_id` are skipped, plus a merge that dedupes by identity |
| P1-3 | a rejected leg stranded later legs in `SUBMITTING` | new terminal `NOT_SUBMITTED` state, `_abort_unsent_legs`, `ORDER_NOT_SUBMITTED` audit, reservations released |
| P2-1 | a bare `assert` in `backend/opaca/` | replaced with `ExecutionInvariantError`; 0 `ast.Assert` across all 40 modules |
| P3-1 | the kill switch was not re-read before `submit_order` | a last-moment read inside `_submit_leg`, marking the leg `NOT_SUBMITTED` |

Two items are reported for the pre-live gate rather than fixed:
**LIVE PRICE READINESS: PRE-LIVE BLOCKER** (the live mutation smoke prices its order
from test constants; no market-data client exists; a wrong `reference_price` flips
the authority decision from `APPROVAL_REQUIRED` to `AUTO`) and
**SCHEMA V2 READINESS: FRESH-DB REQUIRED** (a v1 database fails closed on open and
there is no migration step).

Five probes in the earlier suites were **retargeted**, not relaxed, because the
phase boundary legitimately moved: the "no mutation anywhere" scan became a scope
assertion on call sites and receivers; the dynamic-dispatch allow-list gained the
Phase 3 guard function; two schema assertions pinned to a literal `1` now compare
against `SCHEMA_VERSION`; and the two Phase 2 findings listed above were inverted.

Full reports: `claude/paper-execution-redteam-79a7b1b.md` (architecture review)
and `claude/paper-execution-retest-cd3dc86.md` (final closeout).

---

## Pre-live readiness — bounded live-paper pricing and preflight @ 11d1cde

Target: `origin/feat/prelive-readiness @ 11d1cdeb4d283ba68264823e500ec14c58bf7324`
(`feat: add bounded live-paper pricing and preflight`), on production baseline
`main @ da4a55ff5eb5d0f11cb5fbdcaec8a5f25aba21d9` (tag `paper-execution-complete`),
which is the target's direct parent — the branch is a single commit.

    git worktree add --detach /tmp/pl 11d1cdeb4d283ba68264823e500ec14c58bf7324
    OPACA_BACKEND=/tmp/pl/backend pytest -q redteam/prelive_11d1cde
    #   -> 156 passed, 5 failed   (all five are FINDING markers)
    OPACA_BACKEND=/tmp/pl/backend pytest -q redteam/
    #   -> 936 collected, 16-17 failed  (5 Phase 3 pre-live markers
    #      + 5 findings here + 6 carried Phase 2 P2/P3, plus the
    #      intermittent demo-DB race observation)

**161 collected / 156 passed / 5 findings.** Verdict: **PASS WITH FINDINGS**,
**FIX THEN RETEST** — not merged. **First paper trade readiness: NOT READY.**

This is the final gate before a real order. Reviewed **offline only** — no
credentials requested, no live call, and **no broker mutation of any kind**,
including against the paper endpoint.

Builder gates all reproduce at the target: 407 collected / 404 passed / 3 skipped
(the three live smokes, correctly gated), `ruff check` clean, `ruff format
--check` clean on 85 files, `mypy --strict` clean on 85 files, `git diff --check`
exit 0, 0 bare `assert` across 49 production modules, green under `python -O`
and `-OO`, a credential scan with **0** literals across 114 tracked files, and a
mutation capability scan showing the surface is still **two call sites**, both in
`execution/service.py`, both on `mutate_gateway`.

| area | verdict |
| --- | --- |
| canonical live price source (Alpaca IEX latest trade) | PASS |
| price validation / freshness (15 s) | PASS |
| price TOCTOU | **FAIL** |
| $0.01 reference-price attack | **BLOCKED** |
| canonical price binding | PASS on the bound path; not a precondition |
| bounded BUY limit (10 bps) | PASS |
| maximum cash exposure (`qty × LIMIT`) | PASS |
| fresh schema-v2 demo DB | PASS |
| read-only preflight | PASS |
| Phase 3 execution regression | PASS |
| paper-only mutation boundary | PASS |

Two P1 items are named as pre-live blockers, both new in this branch:

1. **P1-1** — no quote-freshness re-validation at the final mutation boundary.
   `_submit_leg` reads only the kill switch. With 16.0 s of real elapsed time
   injected into the pre-submit window the broker was still mutated
   (`submit_calls == 1`, required 0). `execution/service.py` never reads a wall
   clock, and the documented live smoke freezes `now` **before** the quote fetch
   and then makes 13 broker round-trips before submitting.
2. **P1-2** — the canonical binding is opt-in. `price_bindings` defaults to
   `None`, reducing the guard to "the caller's two numbers agree with each
   other". A matched invented pair at $0.01 reaches AUTO for 1,000,000 SGOV and
   is submitted as a DAY LIMIT at the invented price.

Plus **P2-1** (all four paper-endpoint guards are unanchored `startswith`, so
`paper-api.alpaca.markets.evil.com` is accepted — and `test_s18` in
`paper_execution_79a7b1b/` currently asserts that it *is*), **P2-2** (five
market-data adapter tests skip silently when `pytz` is absent), and three P3
residuals recorded in the full report.

Three of the five pre-live markers in `paper_execution_79a7b1b/` **invert** at
this commit, which is the evidence the fixes are real: the package now has a
market-data client, the live smoke no longer prices from `DEFAULT_PRICES`, and an
understated `reference_price` no longer flips the authority decision to AUTO.

Full report: `claude/prelive-readiness-redteam-11d1cde.md`.
---

## Final pre-live closeout — the last four blockers @ 193d7a2

Target: `origin/feat/prelive-readiness @ 193d7a21cc956d2688f69e339cb79fc44cd34380`
(`fix: close final pre-live execution blockers`), whose **direct parent** is the
previously reviewed `11d1cde` — the retest diff is a single commit: 18 files,
+901/-92, of which 6 are production modules.

    git worktree add --detach /tmp/cl 193d7a21cc956d2688f69e339cb79fc44cd34380
    OPACA_BACKEND=/tmp/cl/backend pytest -q redteam/closeout_193d7a2
    #   -> 312 passed          (~18 s: test_r1 injects 16 s of real elapsed time)
    OPACA_BACKEND=/tmp/cl/backend pytest -q redteam/
    #   -> 1160 passed, 88 failed

**312 collected / 312 passed / 0 findings.** Verdict: **PASS**, **MERGE**
recommended. First paper trade: **READY FOR HUMAN PREFLIGHT**.

Reviewed **offline only** — no credentials requested, no live call, and **no
broker mutation of any kind**, including against the paper endpoint. Nothing was
merged and no production code was modified: all 116 tracked blobs of the target
tree hash-match `git ls-tree 193d7a21` at the end of the session.

All four named blockers are **CLOSED**, each proven by probes that fail at the
parent commit `11d1cde` (14/14, 13/14, a whole file that will not import, 5/5
and 11/14 respectively):

| blocker | verdict | evidence |
| --- | --- | --- |
| **P1-1** quote freshness at the final mutation boundary | **CLOSED** | `_submit_leg` reads a real `datetime.now(UTC)` and revalidates the bound quote immediately before `submit_order`, kill switch on both sides. 14.999 s allowed; **15.000 s allowed** (the documented inclusive maximum); 15 s + 1 µs, 16 s and a future quote all 0 submits; a genuine `time.sleep(16.0)` in the pre-submit window gives 0 submits where the same setup without it submits once. Every failure is `NOT_SUBMITTED`, never `UNKNOWN`. |
| **P1-2** mandatory canonical binding | **CLOSED** | Driven through the **real** `AlpacaPaperExecutionGateway`: `None`, `{}`, incomplete, wrong symbol, wrong quantity, mismatched quote and the invented matched `$0.01` pair (BUY 100,000 and the marketable SELL) all give **0 submits and zero execution rows**; a valid binding proceeds as a DAY LIMIT at 100.80. |
| **P2-1** exact paper endpoint | **CLOSED** | 53 hostile URL forms × 5 production guards, all rejected; zero `startswith` endpoint tests remain anywhere in the 49 production modules. |
| **P2-2** market adapter test gate | **CLOSED** | `pytz==2025.2` pinned in `requirements-dev.txt` and the `paper` extra; zero `importorskip` in `tests/`; without `pytz` the 5 market-adapter tests now **fail** where at `11d1cde` they silently skipped. |

**UNBOUND RESERVATION CAN REACH REAL SUBMIT: NO.** `evaluate_and_reserve` still
supports the offline/unbound mode and still returns `is_auto=True` for an honest
unbound proposal *and* for the invented `$0.01` pair — permitted by the brief
only because every real mutating path independently requires a complete
canonical binding, which is measured: 0 submits against the real gateway on
first call and on replay, and the same refusal on the offline gateway.
Structurally, `submit_order` has one call site, `_submit_leg` has one caller, and
that call passes `price_bindings`.

Builder gates at the target: 447 collected / 444 passed / 3 skipped (the three
live opt-ins only, +40 tests vs `11d1cde` with none removed), `ruff check` clean,
`ruff format --check` clean on 87 files, `mypy --strict` clean on 87 files,
`git diff --check` exit 0, 0 bare `assert` across 49 production modules, green
under `python -O` and `-OO`, a credential scan with 0 literals, and a mutation
capability scan showing two call sites, both on `mutate_gateway`.

### Reading the 88 failures in a verbatim whole-tree run

**70 of them are the new mandatory-binding call contract, not regressions.**
Suites written before this commit call `execute_reserved_proposal` with no
bindings and a frozen `now`, so they block before the broker and can no longer
observe what they were written to attack.
`closeout_193d7a2/contract_adapter.py` supplies **only** that missing
precondition — a canonical binding derived from the `prices` each test already
passes, and a boundary clock pinned to the test's own `now`. Applied to a copy
of the tree (never in place), the result is:

    cp -r <repo> /tmp/adapted
    cat redteam/closeout_193d7a2/contract_adapter.py >> /tmp/adapted/redteam/conftest.py
    cd /tmp/adapted
    OPACA_BACKEND=/tmp/cl/backend pytest -q redteam/ --ignore=redteam/closeout_193d7a2
    #   -> 918 passed, 18 failed   (occasionally 19 — the intermittent P3-2 race)

No assertion was touched and no adversarial check was weakened. Baseline control
at `11d1cde` is 16 failed / 920 passed, so the only genuinely new failures are
three intentional inversions: `test_s18_only_the_paper_endpoint_prefix_is_accepted`
(which asserted the look-alike host *was* acceptable) and the two
`test_s3_toctou.py` structural probes that asserted the boundary read no clock
and ran no validation. Those two are **replaced** by semantic assertions in
`closeout_193d7a2/test_r1_freshness_boundary.py`.

The adapter cannot be used to judge P1-1 or P1-2 — it manufactures a canonical
quote out of caller prices and pins the boundary clock, which are exactly the
two things those blockers are about. The `closeout_193d7a2/` suite runs
**unshimmed**; that is where both P1 verdicts come from.

### Residuals

No new fail-open path. Carried unchanged: P3-1, P3-2 (intermittent), P3-3 and
the six recorded backlog markers. New non-blocking observations: `alpaca-py`
coerces the exact decimal limit price to a binary `float` at the SDK boundary;
`_submit_leg` revalidates the freshness of the binding present at that instant
rather than re-running the full binding check (no economic effect); the live
smoke still captures `now` before the quote fetch, which is no longer fatal; and
an unbound `is_auto=True` is easy for an operator to misread as authority.

The standing non-code item is unchanged: **nothing in this layer has met a real
broker.** A human should run `python -m opaca preflight` and `pytest
--live-paper` against real credentials before anything mutating is contemplated.

Full report: `claude/prelive-final-closeout-193d7a2.md`.
