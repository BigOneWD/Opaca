# Phase 2 red-team probes — reconciliation state + SQLite atomic reservation

Adversarial tests written to **falsify** the builder's report for the Phase 2
reconciliation/persistence/orchestration layer.

| pass | commit | result |
| --- | --- | --- |
| initial review | `3fdabf3bcbe3e0d8c8ccfb5a9feedad584c4b6e2` | 190 passed / 21 findings — PASS WITH FINDINGS, FIX THEN RETEST |
| remediation retest | `d85a2e62b5d4c3852dcd5322eb4d2c907fbec32e` | 372 passed / 9 findings — PASS WITH FINDINGS |
| final closeout retest | `624439fbba9a2f70110e4c413a7783eda564418a` | **409 passed / 8 findings** — PASS, merge recommended |

Written by the reviewer, not the builder. No production code was modified while
producing them. Like the rest of `redteam/`, these tests live only on
`review/treasury-red-team`, are never added to a builder branch, and import
nothing from this branch — they run against a checkout of the reviewed commit.

## Running

    git worktree add --detach /tmp/rc 624439fbba9a2f70110e4c413a7783eda564418a
    OPACA_BACKEND=/tmp/rc/backend pytest -q redteam/reconciliation_3fdabf3

`redteam/conftest.py` puts `$OPACA_BACKEND` and `$OPACA_BACKEND/tests` on
`sys.path`. Toolchain: CPython 3.11.15, pytest 9.1.1 (the pins in
`backend/requirements-dev.txt`).

## Reading the result

    409 passed, 8 failed

Every failure is a deliberate `pytest.fail("FINDING …")` marker placed **after**
its invariant assertions have all held; the failure message *is* the finding.

    pytest -q redteam/reconciliation_3fdabf3 -k "not FINDING"   # -> 409 passed

Both figures are deterministic across repeated runs and identical under
`python -O` / `-OO`.

## Teeth

Two independent teeth checks:

* `test_teeth.py` neutralises the reservation mechanism and shows the P0-A / P0-B
  concurrency assertions then fail — 60 + 60 concurrent sells reserve 120 shares
  of a 100-share position, and two buys deploy 30,005.62 against 22,000.
* Run the **current** suite against each earlier commit:

        OPACA_BACKEND=<worktree-at-d85a2e6>/backend pytest -q redteam/reconciliation_3fdabf3
        #   -> 23 failed   (15 of them close the replay-eligibility fix)
        OPACA_BACKEND=<worktree-at-3fdabf3>/backend pytest -q redteam/reconciliation_3fdabf3
        #   -> 109 failed

  Every inverted test fails at the commit it was written against and passes at
  the commit that fixed it, so none of the inversions is vacuous.

## Files

| file | attack class |
| --- | --- |
| `test_retest_624439f.py` | the final closeout retest: historical AUTO is not current execution eligibility |
| `test_retest_d85a2e6.py` | the narrow remediation retest: P0-1, P0-2, P1-1 … P1-5 and the named spot-check invariants |
| `test_p0a_sell_race.py` | P0-A atomic SELL reservation race — threads and two real OS processes |
| `test_p0b_cash_race.py` | P0-B cash / deployment / rolling-authority concurrency |
| `test_p0c_stale.py` | P0-C snapshot versioning, staleness, snapshot age |
| `test_p0d_idempotency.py` | P0-D retry, replay, `client_order_id` collision, capacity neutrality |
| `test_p0d2_replay_gates.py` | P0-D the gates the idempotent-replay branch must apply |
| `test_p1a_adapter.py` | P1-A malformed broker payloads |
| `test_p1b_recon_states.py` | P1-B reconciliation state distinctions, fail-closed classification |
| `test_p1c_seed.py` | P1-C scenario seed-once and seed transactionality |
| `test_p1d_sqlite.py` | P1-D WAL, foreign keys, atomicity, Decimal / timestamp round-trip, schema version |
| `test_p1ef_audit_approval.py` | P1-E audit trail; P1-F `APPROVAL_REQUIRED` and expiry |
| `test_p1g_readonly.py` | P1-G read-only Alpaca capability (AST + runtime) |
| `test_p1h_failure_injection.py` | P1-H failure at every transaction boundary |
| `test_p2_quality.py` | P2 architecture / test-quality observations |
| `test_teeth.py` | proves the P0 probes detect a real oversell |
| `probe_support.py` | shared concurrency harness and reservation accounting |

## Findings closed at d85a2e6

All seven findings named for retest are CLOSED, each verified by tests that fail
at `3fdabf3`:

| finding | tests failing at 3fdabf3 |
| --- | --- |
| P0-1 replay safety | 6 + 7 |
| P0-2 snapshot freshness | 8 + 2 |
| P1-1 read-only capability | 14 + 4 |
| P1-2 invalid broker state | 8 + 5 |
| P1-3 approval expiry | 8 + 2 |
| P1-4 `quantity_available` drift | 4 + 1 |
| P1-5 scenario seed transaction | 4 + 2 |

## Findings still open (8 markers)

**P0-1-r is CLOSED at `624439f`.** `OrchestrationResult.is_auto` now returns
False whenever `idempotent_replay` is True, so a replayed proposal never
asserts current execution eligibility. Replay still preserves the historical
authority result and reservation rows and still consumes no new capacity.
15 tests covering this fail at `d85a2e6`.

One P3 residual and seven P2 observations remain, none fail-open. All of them
are now recorded by the builder in `docs/backlog.md`:

* **P1-1-r** (`test_p1g_readonly.py`, P3) — the retained bound read methods carry
  `__self__`, so the TradingClient is one introspection hop away from a gateway
  that passes the guard. No call site; nothing is invoked by the probe.
* reservations are never released; one AUTO sell locks out later buys of that
  symbol; `proposal_hash` is leg-order sensitive; three store mutators run
  outside transactions; a COMMIT failure leaves the connection in a
  transaction; the mutation scan excludes `spike/`; the backend test gate is
  not hermetic.

Full reports: `claude/reconciliation-state-redteam-3fdabf3.md` (initial review),
`claude/reconciliation-state-retest-d85a2e6.md` (remediation retest) and
`claude/reconciliation-state-closeout-624439f.md` (this closeout) at the
repository root.
