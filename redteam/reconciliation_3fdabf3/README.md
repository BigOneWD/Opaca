# Phase 2 red-team probes — reconciliation state + SQLite atomic reservation

Adversarial tests written to **falsify** the builder's report for

    origin/feat/reconciliation-state @ 3fdabf3bcbe3e0d8c8ccfb5a9feedad584c4b6e2

Written by the reviewer, not the builder. No production code was modified while
producing them. Like the rest of `redteam/`, these tests live only on
`review/treasury-red-team`, are never added to a builder branch, and import
nothing from this branch — they run against a checkout of the reviewed commit.

## Running

    git worktree add --detach /tmp/rc 3fdabf3bcbe3e0d8c8ccfb5a9feedad584c4b6e2
    OPACA_BACKEND=/tmp/rc/backend pytest -q redteam/reconciliation_3fdabf3

`redteam/conftest.py` puts `$OPACA_BACKEND` and `$OPACA_BACKEND/tests` on
`sys.path`. Toolchain: CPython 3.11.15, pytest 9.1.1 (the pins in
`backend/requirements-dev.txt`).

## Reading the result

    190 passed, 21 failed

Every failure is a deliberate `pytest.fail("FINDING …")` marker placed **after**
its invariant assertions have all held; the failure message *is* the finding.
This is the same convention the treasury-core suite uses for open findings.

    pytest -q redteam/reconciliation_3fdabf3 -k "not FINDING"   # -> 190 passed

gives the pure invariant suite. Both figures are deterministic across repeated
runs and identical under `python -O` / `-OO`.

## Teeth

`test_teeth.py` proves the P0-A and P0-B results are detections rather than
vacuous passes. With the reservation mechanism monkeypatched out
(`sell_reservations` and `_cash_reservation_obligations` neutralised):

* two concurrent `SELL 60` proposals both reach AUTO and reserve **120 shares of
  a 100-share position**;
* two concurrent 15,002.81 BUY proposals deploy **30,005.62 against 22,000** of
  deployable cash.

Both teeth checks pass, so the green concurrency results have teeth.

## Files

| file | attack class |
| --- | --- |
| `test_p0a_sell_race.py` | P0-A atomic SELL reservation race — threads and two real OS processes |
| `test_p0b_cash_race.py` | P0-B cash / deployment / rolling-authority concurrency |
| `test_p0c_stale.py` | P0-C snapshot versioning, staleness, snapshot age |
| `test_p0d_idempotency.py` | P0-D retry, replay, `client_order_id` collision, capacity neutrality |
| `test_p0d2_replay_gates.py` | P0-D what the idempotent-replay branch skips |
| `test_p1a_adapter.py` | P1-A malformed broker payloads (account / positions / assets / orders / prices / timestamps) |
| `test_p1b_recon_states.py` | P1-B reconciliation state distinctions, fail-closed classification |
| `test_p1c_seed.py` | P1-C scenario seed-once |
| `test_p1d_sqlite.py` | P1-D WAL, foreign keys, atomicity, Decimal / timestamp round-trip, schema version |
| `test_p1ef_audit_approval.py` | P1-E audit trail; P1-F `APPROVAL_REQUIRED` |
| `test_p1g_readonly.py` | P1-G read-only Alpaca guarantee (AST scan + runtime) |
| `test_p1h_failure_injection.py` | P1-H failure at every transaction boundary |
| `test_p2_quality.py` | P2 architecture / test-quality observations |
| `test_teeth.py` | proves the P0 probes detect a real oversell |
| `probe_support.py` | shared concurrency harness and reservation accounting |

## Findings

21, none fail-open in a way that can currently place a trade (no broker
execution path exists at this commit).

| id | probe | summary |
| --- | --- | --- |
| P0-1 | `test_p0d2_replay_gates.py` (4) | the idempotent-replay branch returns `is_auto=True` with the reconciliation-status, stale-snapshot and kill-switch gates all skipped |
| P0-2 | `test_p0c_stale.py` (2) | no maximum snapshot age; `expected_snapshot_version` is optional |
| P1-1 | `test_p1g_readonly.py` (2) | the read-only gateway retains a mutable `TradingClient` on `_client`; the blacklist misses `cancel_order_by_id` / `replace_order_by_id` |
| P1-2 | `test_p1b_recon_states.py` (3) | duplicate broker rows and `filled > quantity` escape as raw exceptions instead of `INVALID_BROKER_STATE` |
| P1-3 | `test_p1ef_audit_approval.py` (1) | `APPROVAL_REQUIRED` expiry is recorded but never enforced |
| P1-4 | `test_p1b_recon_states.py` (1) | unexplained broker hold-aside produces no drift when no local reservation exists |
| P1-5 | `test_p1c_seed.py`, `test_p1d_sqlite.py` (2) | `seed_scenario_once()` and the public store mutators are non-transactional off the autocommit connection |
| P2 | `test_p2_quality.py`, `test_p1h_failure_injection.py` (6) | reservations never released; one AUTO sell permanently locks out later buys of that symbol; `proposal_hash` is leg-order sensitive; mutation-scan scope; non-hermetic test gate; COMMIT failure leaves the connection in a transaction |

Full report: `claude/reconciliation-state-redteam-3fdabf3.md` at the repository
root. `REPORT.md` here points at it.
