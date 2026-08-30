# Phase 3 red-team probes — paper execution lifecycle

Adversarial tests written to **falsify** the builder's report for

    origin/feat/paper-execution @ 79a7b1b837c86dc700533eeda6f5699b197a7d4a

on production baseline `main @ 12f4eb02f9c832f7368cc0c06f67afaf4bb1d7d8`, and
retested at

    origin/feat/paper-execution @ cd3dc86b7153718bbc98072a79a81ae3587f9477

| pass | commit | result |
| --- | --- | --- |
| full architecture review | `79a7b1b` | 94 passed / 7 findings — PASS WITH FINDINGS, FIX THEN RETEST |
| final narrow retest | `cd3dc86` | **135 passed / 0 execution-safety findings** — all five closed |

Teeth for the retest: the current suite run against `79a7b1b` fails **30** tests
that pass at `cd3dc86`.

This is the first phase that can place an order. The probes are built on the
production doubles (`FakeAlpacaGateway`, `FakePaperExecutionGateway`) rather
than the builder's `tests/execution_helpers.py`, so a defect in the builder's
fixtures cannot mask a defect in the layer under test.

**No live call and no mutation of any kind was performed.** Every probe runs
offline against an isolated SQLite store.

## Running

    git worktree add --detach /tmp/pe cd3dc86b7153718bbc98072a79a81ae3587f9477
    OPACA_BACKEND=/tmp/pe/backend pytest -q redteam/paper_execution_79a7b1b
    #   -> 139 passed, 4 failed   (all four are PRE-LIVE readiness reports)
    OPACA_BACKEND=/tmp/pe/backend pytest -q redteam/paper_execution_79a7b1b \
        --ignore=redteam/paper_execution_79a7b1b/test_prelive_readiness.py
    #   -> 135 passed            (execution safety: no findings)

Every failure is a deliberate `pytest.fail("FINDING …")` marker placed after
its invariant assertions have all held; the failure message *is* the finding.

    pytest -q redteam/paper_execution_79a7b1b -k "not FINDING"   # -> 94 passed

## Files

| file | attack class |
| --- | --- |
| `test_p0_submission.py` | at most one broker order per logical leg: retry, concurrency, lost responses, crash between intent and submit, pre-submit gates, paper-endpoint boundary |
| `test_p0_reservations.py` | reservations shrink only against proven disposition; partial fill, cancel-with-remainder, rejection, oversell |
| `test_p0_recon_weakening.py` | bounds on the new "our own fill explains the position delta" exception |
| `test_p1_lifecycle.py` | state machine, recovery, human approval, audit, corrupt broker acknowledgements |
| `test_p1_multileg.py` | multi-leg proposals, cross-proposal concurrency, TOCTOU windows |
| `p3_support.py` | the offline world, built from production doubles |

## Findings

All five findings from the `79a7b1b` review are **CLOSED at `cd3dc86`**:

| id | summary | closed by |
| --- | --- | --- |
| P1-2 | a live order counted twice against its own capacity | broker orders with a locally known `client_order_id` are skipped, plus a merge that dedupes by identity |
| P1-1 | `compare_state` skipped the explained-delta comparison at a zero raw delta | `delta == expected` is now compared for every symbol, including symbols that appear only in the explained set |
| P1-3 | a rejected leg stranded later legs in `SUBMITTING` | new terminal `NOT_SUBMITTED` state, `_abort_unsent_legs`, `ORDER_NOT_SUBMITTED` audit, reservations released |
| P2-1 | a bare `assert` in the production package | replaced with `ExecutionInvariantError`; 0 `ast.Assert` across all 40 modules |
| P3-1 | the kill switch was not re-read before `submit_order` | a last-moment read inside `_submit_leg`, marking the leg `NOT_SUBMITTED` |

`test_prelive_readiness.py` is a **report, not a defect list** — its four failures
answer the two pre-live questions and are deliberately not fixed:

* **LIVE PRICE READINESS: PRE-LIVE BLOCKER.** The live mutation smoke prices its
  order from `tests.helpers.DEFAULT_PRICES`, there is no market-data client in the
  package, and nothing binds a leg's `reference_price` to `context.prices`. The
  same 300-share BUY is `APPROVAL_REQUIRED` at the real price and **AUTO** at a
  wrong one.
* **SCHEMA V2 READINESS: FRESH-DB REQUIRED.** A v1 database fails closed on open
  and there is no migration step.

Full reports: `claude/paper-execution-redteam-79a7b1b.md` (architecture review)
and `claude/paper-execution-retest-cd3dc86.md` (this retest).
