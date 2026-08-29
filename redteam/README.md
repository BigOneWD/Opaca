# Opaca — red-team suite

Two phases of adversarial review live here:

* `redteam/*.py` + `closeout_bc5fcda/` — **Treasury Core** (`feat/treasury-core`)
* `reconciliation_3fdabf3/` — **Phase 2 reconciliation state + SQLite atomic
  reservation** (`feat/reconciliation-state`)

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
