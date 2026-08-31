# Final closeout red-team probes — the last four pre-live blockers

Adversarial tests written to **falsify** the builder's claim that the four
named pre-live blockers are closed at

    origin/feat/prelive-readiness @ 193d7a21cc956d2688f69e339cb79fc44cd34380

(`fix: close final pre-live execution blockers`), whose **direct parent** is the
previously reviewed `11d1cdeb4d283ba68264823e500ec14c58bf7324` — the retest diff
is a single commit: 18 files, +901/-92, of which 6 are production modules.

**312 collected / 312 passed.** Verdict: **PASS**, **MERGE** recommended.
First paper trade: **READY FOR HUMAN PREFLIGHT**.

Reviewed **offline only** — no credentials requested (`APCA_*` absent from the
environment throughout), no live call, and **no broker mutation of any kind**,
including against the paper endpoint. Nothing was merged and no production code
was modified: all 116 tracked blobs of the target tree hash-match
`git ls-tree 193d7a21` at the end of the session.

## Running

    git worktree add --detach /tmp/cl 193d7a21cc956d2688f69e339cb79fc44cd34380
    OPACA_BACKEND=/tmp/cl/backend pytest -q redteam/closeout_193d7a2
    #   -> 312 passed        (~18 s: test_r1 injects 16 s of real elapsed time)

    OPACA_BACKEND=/tmp/cl/backend pytest -q redteam/
    #   -> 1160 passed, 88 failed
    #      88 = 18 open markers + 70 pre-193d7a2 suites that predate the
    #      mandatory-binding call contract; see contract_adapter.py

Requires `backend/requirements-dev.txt`, which at this commit pins
`alpaca-py==0.33.0` and `pytz==2025.2` — finding P2-2 from the previous review
is what added them. Gate interpreter: CPython 3.11.

| file | attack class | tests |
| --- | --- | --- |
| `test_r1_freshness_boundary.py` | P1-1 — a real wall clock and a revalidation of the exact bound canonical quote immediately before `submit_order` | 14 |
| `test_r2_binding_mandatory.py` | P1-2 — a complete canonical binding as a precondition of real broker mutation, driven through the real `AlpacaPaperExecutionGateway` | 14 |
| `test_r3_paper_endpoint.py` | P2-1 — exact parsed endpoint validation: 53 hostile URL forms x 5 production guards | 265 |
| `test_r4_market_gate.py` | P2-2 — `pytz` pinned, no `importorskip`, market-adapter tests fail rather than skip | 5 |
| `test_r5_regressions.py` | critical-regression spot-checks under the new contract | 14 |

`closeout_support.py` is the offline world plus the closeout harness. It is
deliberately **not** named `support.py`: that basename is already used by
`prelive_11d1cde/`, and pytest's prepend import mode would bind whichever suite
was collected first.

## Teeth

Run unchanged against the parent commit `11d1cde`, the suite falls over:

| file | at 11d1cde |
| --- | --- |
| `test_r1_freshness_boundary.py` | **14 / 14 fail** |
| `test_r2_binding_mandatory.py` | **13 / 14 fail** |
| `test_r3_paper_endpoint.py` | **does not import** — `is_exact_paper_endpoint` does not exist |
| `test_r4_market_gate.py` | **5 / 5 fail** |
| `test_r5_regressions.py` | **11 / 14 fail** |

## What each blocker now does

**P1-1 — CLOSED.** `_submit_leg` reads `boundary_now = _utc_now()` (a real
`datetime.now(UTC)`) and re-runs `validate_canonical_quote` against the bound
quote immediately before `submit_order`, with the kill switch read on both sides
of that window. Measured: 14.999 s allowed; **15.000 s allowed** — the
documented inclusive maximum (`age > max_age`), now also pinned by a builder
test; 15 s + 1 microsecond, 16 s and a future quote all **0 submits**; a
**genuine `time.sleep(16.0)`** injected into the last pre-submit window gives
**0 submits** where the identical setup without the delay submits once; a kill
switch flipped inside the same window gives 0 submits; a stale quote swapped
into the bindings map after the intent commit gives 0 submits. Every
pre-mutation failure is `NOT_SUBMITTED` with a specific reason and an empty
`load_unknown_orders()` — never `UNKNOWN`.

Two probes from `prelive_11d1cde/test_s3_toctou.py` encoded the defect
(`test_the_submit_boundary_reads_the_kill_switch_but_not_the_clock`,
`test_the_whole_execution_path_never_reads_a_wall_clock`) and now fail. They are
**replaced** here by semantic assertions, not preserved.

**P1-2 — CLOSED.** `_persist_submission_intents` refuses `None` and `{}`
bindings before anything else; `_submit_leg` independently refuses a missing or
wrong-symbol binding at the boundary. Every probe drives the **real production
class** `opaca.broker.paper_execution.AlpacaPaperExecutionGateway`, constructed
over an in-process stub client, so `verify_paper_client`,
`require_paper_endpoint`, `assert_paper_execution_gateway`, the `__slots__`
narrowing and the real `LimitOrderRequest` construction all execute. `None`,
`{}`, incomplete, wrong symbol, wrong quantity, mismatched quote, and the
invented matched `$0.01` pair (BUY 100,000 and the marketable SELL) all give
**0 submits and zero execution rows**; a valid canonical binding proceeds as a
DAY LIMIT at 100.80.

**UNBOUND RESERVATION AUTO IS NOT EXECUTION AUTHORITY.**
`evaluate_and_reserve` still supports the offline/unbound mode and still returns
`is_auto=True` for an honest unbound proposal *and* for the invented `$0.01`
pair. The brief permits that only if every real mutating path independently
requires a complete canonical binding — which is what is measured: both
reservations give 0 submits against the real gateway, on first call and on
replay, and the same refusal holds on the offline gateway. Structurally,
`submit_order` has one call site, inside `_submit_leg`; `_submit_leg` has one
caller; that call passes `price_bindings`.

**P2-1 — CLOSED.** All four guards delegate to `require_paper_endpoint`, which
parses the URL and requires scheme `https`, host exactly
`paper-api.alpaca.markets`, no port, no userinfo, no query, no fragment, and
path in `{"", "/"}`. 265 probes: look-alike and sibling hosts, the live host,
`http://`, scheme-relative and bare host, localhost and loopback, userinfo
tricks in both directions, four port forms, path / query / fragment / backslash
forms, and ten malformed or whitespace-padded inputs — all rejected by all five
entry points. An AST sweep of the 49 production modules finds **zero**
`startswith` calls near an endpoint, base URL or `alpaca.markets` constant. The
Phase 3 probe that asserted the look-alike *was* acceptable
(`test_s18_only_the_paper_endpoint_prefix_is_accepted[...evil.com]`) now fails,
correctly, and the builder removed its in-tree copy.

**P2-2 — CLOSED.** `pytz==2025.2` and `alpaca-py==0.33.0` are pinned in both
`requirements-dev.txt` and the `paper` extra; the `importorskip` autouse fixture
is gone; zero `importorskip` anywhere in `tests/`; exactly three
`pytest.mark.skip` declarations, all in `tests/conftest.py`, all live opt-ins.
Measured both ways in an environment without `pytz` (`alpaca-py` does not pull
it transitively, so the pin is load-bearing): at `11d1cde` **5 silent skips**;
at `193d7a2` the **same 5 tests fail loudly**.

## Regressions

Duplicate-order prevention (5 retries and 4 concurrent executors -> 1 submit),
deterministic `client_order_id` and reservation/broker correlation, lost ACK ->
UNKNOWN and three recoveries without resubmission, the final kill switch, BUY
reservation at exactly `qty x LIMIT`, SELL oversell refusal, partial fills,
bounded DAY LIMIT above the canonical print, the two-call-site mutation surface,
a live-endpoint gateway raising with 0 submits, zero bare asserts, and the
read-only preflight naming no mutator — all **PASS**.

Builder gates at the target: 447 collected / 444 passed / 3 skipped (the three
live opt-ins only), `ruff check` clean, `ruff format --check` clean on 87 files,
`mypy --strict` clean on 87 files, `git diff --check` exit 0, 0 bare `assert`
across 49 production modules, green under `python -O` and `-OO`, a credential
scan with 0 literals, and a mutation capability scan showing two call sites,
both on `mutate_gateway`.

## Residuals

No new fail-open path. Carried unchanged: P3-1 (a failed demo-DB init leaves a
blocking file), P3-2 (the racy demo-DB overwrite refusal — it reproduced in 1 of
4 measured runs, so the tree occasionally reports 19 markers instead of 18),
P3-3 (`max_buy_cash_obligation` rounds down), and the six recorded backlog
markers.

New observations, none blocking: `alpaca-py` coerces the exact decimal limit
price to a binary `float` at the SDK boundary (conservative in direction for a
BUY, but money crosses the last boundary as a float); `_submit_leg` revalidates
the freshness of whatever binding is in the map at that instant rather than
re-running the full binding check (no economic effect — the leg's limit is fixed
and was fully bound in the intent transaction); the live mutation smoke still
captures `now` before the quote fetch, which is no longer fatal; and
`evaluate_and_reserve` still returns `is_auto=True` for an unbound invented
pair, which is permitted but easy for an operator to misread as authority.

The standing non-code item is unchanged: **nothing in this layer has met a real
broker.** `test_live_paper_readonly.py` and `python -m opaca preflight` should
be run against real credentials, by a human at a keyboard, before anything
mutating is contemplated.

Full report: `claude/prelive-final-closeout-193d7a2.md`.
