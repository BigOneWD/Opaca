# Phase 3 red-team probes — paper execution lifecycle

Adversarial tests written to **falsify** the builder's report for

    origin/feat/paper-execution @ 79a7b1b837c86dc700533eeda6f5699b197a7d4a

on production baseline `main @ 12f4eb02f9c832f7368cc0c06f67afaf4bb1d7d8`.

This is the first phase that can place an order. The probes are built on the
production doubles (`FakeAlpacaGateway`, `FakePaperExecutionGateway`) rather
than the builder's `tests/execution_helpers.py`, so a defect in the builder's
fixtures cannot mask a defect in the layer under test.

**No live call and no mutation of any kind was performed.** Every probe runs
offline against an isolated SQLite store.

## Running

    git worktree add --detach /tmp/pe 79a7b1b837c86dc700533eeda6f5699b197a7d4a
    OPACA_BACKEND=/tmp/pe/backend pytest -q redteam/paper_execution_79a7b1b
    #   -> 94 passed, 7 failed

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

| id | probes | summary |
| --- | --- | --- |
| P1-1 | `test_p0_recon_weakening.py` (2) | `compare_state` skips the explained-delta comparison whenever the raw position delta is zero, so a local fill the broker does not reflect — or an exactly offsetting external movement — raises no drift |
| P1-2 | `test_p0_reservations.py` (2) | a live order is counted twice (its resized reservation **and** the broker order it produced), so every open order costs twice its size in capacity |
| P1-3 | `test_p1_multileg.py` (1) | a REJECTED first leg strands every later leg in `SUBMITTING` for an order that was never sent; recovery can only reclassify it as `UNKNOWN`, jamming the proposal's reservations |
| P2-1 | `test_p1_lifecycle.py` (1) | a bare `assert` reappeared in `backend/opaca/`, regressing a treasury-core control closed at `bc5fcda`. Not reachable from the public path — bounded by the test beside it |
| P3-1 | `test_p1_multileg.py` (1) | the kill switch is not re-read immediately before `submit_order`, so a flip between revalidation and the broker call is not seen |

Full report: `claude/paper-execution-redteam-79a7b1b.md`.
