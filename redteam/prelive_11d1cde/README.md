# Pre-live readiness red-team probes — bounded live-paper pricing and preflight

Adversarial tests written to **falsify** the builder's report for

    origin/feat/prelive-readiness @ 11d1cdeb4d283ba68264823e500ec14c58bf7324

on production baseline `main @ da4a55ff5eb5d0f11cb5fbdcaec8a5f25aba21d9`
(tag `paper-execution-complete`), which is the target's direct parent — the
branch is a single commit.

**161 collected / 156 passed / 5 findings.** Verdict: **PASS WITH FINDINGS**,
**FIX THEN RETEST** — not merged. First paper trade: **NOT READY**.

This is the last gate before a real order. Reviewed **offline only** — no
credentials requested, no live call, and **no broker mutation of any kind**,
including against the paper endpoint. Every probe runs against an isolated
SQLite store and the production doubles (`FakeAlpacaGateway`,
`FakePaperExecutionGateway`, `FakeMarketData`), so a defect in the builder's
fixtures cannot mask a defect in the layer under test.

## Running

    git worktree add --detach /tmp/pl 11d1cdeb4d283ba68264823e500ec14c58bf7324
    OPACA_BACKEND=/tmp/pl/backend pytest -q redteam/prelive_11d1cde
    #   -> 156 passed, 5 failed   (all five are FINDING markers)
    OPACA_BACKEND=/tmp/pl/backend pytest -q redteam/prelive_11d1cde -k "not FINDING"
    #   -> 156 passed

`test_s3_toctou.py` injects 16 s of real elapsed time on purpose; the suite
takes ~18 s.

Requires `pytz` in addition to `backend/requirements-dev.txt` and
`alpaca-py==0.33.0` — see finding P2-2.

| file | attack class | tests |
| --- | --- | --- |
| `test_s1_canonical_source.py` | canonical live price source; IEX feed; read-only data client; no synthetic fallback | 11 |
| `test_s2_freshness.py` | quote validation and the 15 s freshness bound — missing, stale, boundary, future, malformed timezone, zero, negative, NaN, Infinity, non-finite `Decimal`, malformed price | 58 |
| `test_s3_toctou.py` | price TOCTOU across fetch → policy/reserve → intent → pre-submit → `submit_order`, and the kill switch in the same window | 9 |
| `test_s4_binding.py` | the $0.01 reference-price attack and canonical price binding | 14 |
| `test_s5_bounded_limit.py` | 10 bps bounded BUY limit, cent boundaries, `qty × LIMIT` maximum cash exposure, DAY LIMIT submission | 26 |
| `test_s6_demo_db.py` | fresh schema-v2 `opaca-paper-demo.db`: WAL, foreign keys, `db_role`, seed-once, overwrite refusal, v1 fail-closed, wrong-role, concurrency, failed init | 15 |
| `test_s7_preflight.py` | read-only preflight: permitted read set, no reservation/intent/execution row, endpoint fail-closed, credential redaction, CLI | 17 |
| `test_s9_mutation_surface.py` | mutation surface (AST) and Phase 3 execution regressions re-run under LIMIT orders | 12 |

Every failure is a deliberate `pytest.fail(...)` / assertion marker placed
**after** its probe assumptions held; the message is the finding.

| finding | severity | summary |
| --- | --- | --- |
| P1-1 | pre-live blocker | no quote-freshness re-validation between the intent transaction and `submit_order`; 16.0 s of injected drift still produced `submit_calls == 1` where 0 is required. `execution/service.py` never reads a wall clock, and the live smoke freezes `now` before the quote fetch, then makes 13 broker round-trips before submitting |
| P1-2 | pre-live blocker | `price_bindings` is optional; with it `None` a caller-supplied matched pair at $0.01 reaches AUTO for 1,000,000 SGOV and is submitted as a DAY LIMIT at the invented price |
| P2-1 | fail-open | all four paper-endpoint guards are unanchored `startswith`; `https://paper-api.alpaca.markets.evil.com` is accepted by preflight, `assert_paper_execution_gateway` and `verify_paper_client` |
| P3-1 | residual | a failed demo-DB init rolls back the transaction but leaves an empty schema-v2 file that blocks the retry |
| P3-2 | residual | the demo-DB overwrite refusal is a racy `Path.exists()` check (reproduced 2 of 8 runs); seeding is idempotent, so no data is duplicated — the marker is intermittent by nature |

`test_s6_demo_db.py::test_OBSERVATION_the_overwrite_refusal_is_a_racy_existence_check`
is the only non-deterministic probe in the tree. It passes on most runs and
fails when the race is caught; both outcomes are the documented behaviour.

## Teeth

The whole suite fails to import at the baseline `da4a55f` — `opaca.market` does
not exist there — so the layer under test is entirely new:

    OPACA_BACKEND=<worktree-at-da4a55f>/backend pytest -q redteam/prelive_11d1cde
    #   -> 8 errors during collection

The sharper evidence is inversion of the previous review's own probes. Three of
the five pre-live markers in `paper_execution_79a7b1b/test_prelive_readiness.py`
now fail *because the defect is fixed*:

* `test_the_package_contains_no_market_data_client` — `get_latest_trade` exists
* `test_FINDING_the_live_mutation_smoke_prices_the_order_from_test_constants` —
  `DEFAULT_PRICES` is gone from the live smoke
* `test_FINDING_a_wrong_price_changes_the_authority_outcome` — the understated
  300-share BUY no longer reaches AUTO; it is blocked

The other two (`..._a_version_one_database_fails_closed_with_no_migration`,
`..._no_check_binds_reference_price_to_the_price_mapping`) are unconditional
report markers and still stand as status, not as open exploits — the $0.01
attack itself is **BLOCKED**, verified by 12 separate probes in
`test_s4_binding.py`.

## Cross-check at the target

    OPACA_BACKEND=/tmp/pl/backend pytest -q redteam/
    #   -> 936 collected, 16-17 failed
    #      (5 Phase 3 pre-live markers + 5 findings here
    #       + 6 carried Phase 2 P2/P3, plus the intermittent P3-2 race)

Without this suite the tree is 775 collected / 764 passed / 11 failed.

No execution-safety marker appears anywhere in the tree at this commit, and the
214 treasury-core and 409 Phase 2 probes remain green — the pre-live phase
regressed nothing.

Full report: `claude/prelive-readiness-redteam-11d1cde.md`.
