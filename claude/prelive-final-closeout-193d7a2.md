# OPACA pre-live — FINAL 4-blocker closeout retest @ 193d7a2

**RED TEAM VERDICT: PASS**

**TARGET SHA:** `193d7a21cc956d2688f69e339cb79fc44cd34380` — `fix: close final pre-live execution blockers`, tip of `origin/feat/prelive-readiness`
**PREVIOUS REVIEWED TARGET:** `11d1cdeb4d283ba68264823e500ec14c58bf7324` — verified as the target's **direct parent** (`git rev-parse 193d7a21^` → `11d1cde`); the retest diff is a single commit
**PRESERVED RED-TEAM BASELINE:** `review/treasury-red-team @ 52c3d383ea853493de75fa520b055557a3b8c895` — the repository working tree, unchanged at start and end

Reviewed from a byte-verified export of the target tree in a scratch directory outside the repository: all **116** tracked blobs hash-match `git ls-tree 193d7a21`, re-verified at the end of the session with **0** mismatches. `git status --porcelain` → **0** at start and end. HEAD never moved off the red-team baseline.

**Nothing was merged. No production code was modified. No finding was fixed. No PAPER broker mutation of any kind was performed. No credentials were requested or present (`APCA_*` absent from the environment throughout); the three live smokes stayed skipped.**

The retest diff is narrow: **18 files, +901/−92**, of which 6 are production modules — `broker/gateway.py` (+85), `broker/paper.py`, `execution/gateway.py`, `execution/service.py` (+74), `market/binding.py` (docstring), `preflight.py` — plus `pyproject.toml` and `requirements-dev.txt`.

---

## GATES

| gate | result |
| --- | --- |
| builder suite | **447 collected → 444 passed, 3 skipped, 0 failed** (404/3 at `11d1cde` → **+40 tests, none removed**) |
| skips | the three live opt-ins only (`live paper smoke`, `live paper preflight`, `live paper mutation`) |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 87 files already formatted |
| `mypy --strict` | Success: no issues found in **87** source files |
| `git diff --check` | clean, exit 0 (`11d1cde..193d7a2`) |
| credential scan | 369 files → **0 literals** (7 regex hits, all false positives: a 40-char class name, the env-var *name* `APCA_API_SECRET_KEY`, one prose string) |
| mutation capability scan | **2 call sites**, both `execution/service.py`, both on `mutate_gateway` (`cancel_order_by_id`:328, `submit_order`:607); **0** HTTP/socket imports; 49 production modules |
| bare asserts (AST) | **0 `ast.Assert` nodes** across all 49 modules |
| `python -O` / `-OO` | 444 passed / 3 skipped under both (the single warning is pytest's own `-O` notice) |

**Toolchain:** the project's pinned gate set from `requirements-dev.txt` (`mypy==2.3.1`, `pytest==9.1.1`, `ruff==0.16.5`, `alpaca-py==0.33.0`, `pytz==2025.2`) on **CPython 3.11.16** (aarch64 Linux). The previous review used 3.11.15; the patch difference is disclosed and no result depends on it.

## TEST INVENTORY

| suite | result |
| --- | --- |
| builder (`backend/tests`) | 447 → **444 passed**, 3 skipped, 0 failed |
| new closeout retest suite (this review, 5 files) | **312 → 312 passed**, deterministic across 3 consecutive runs |
| preserved `redteam/` tree, contract-adapted | 936 → **918 passed, 18 open markers**, identical across 3 runs |
| preserved `redteam/` tree, verbatim | 936 → 848 passed, 88 failed — 70 of those are purely the new mandatory-binding call contract (see below) |

**Teeth.** The closeout suite discriminates sharply against the parent commit. Run unchanged at `11d1cde`: `test_r1` **14/14 fail**, `test_r2` **13/14 fail**, `test_r3` **fails to import** (`is_exact_paper_endpoint` does not exist there), `test_r4` **5/5 fail**, `test_r5` **11/14 fail**. Nothing here passes by accident.

---

## 1. P1-1 — FINAL MUTATION-BOUNDARY QUOTE FRESHNESS — **CLOSED**

`_submit_leg` now takes `price_bindings`, reads `boundary_now = _utc_now()` (a real `datetime.now(UTC)`), and re-runs `validate_canonical_quote(bound.quote, now=boundary_now)` immediately before `submit_order`, returning `NOT_SUBMITTED` on failure. The kill switch is read on **both** sides of that window.

| required | measured |
| --- | --- |
| 14.999 s old at the mutation boundary | **allowed** — 1 submit |
| exactly 15.000 s | **allowed** — 1 submit; the documented inclusive maximum (`age > max_age`), consistent with `validate_quote_freshness` and now pinned by a builder test (`test_inclusive_fifteen_second_boundary`) |
| 15 s + 1 µs | **0 submits**, `NOT_SUBMITTED` |
| 16 s | **0 submits** |
| future quote at the boundary | **0 submits** |
| **16.0 s of real injected wall time** after the intent transaction validated | **0 submits** (control run with the identical setup and no delay: **1 submit**) |
| final kill-switch flip inside the same window | **0 submits**, reason `kill switch active immediately before submit` |
| exact bound quote revalidated (stale quote swapped into the map after the intent commit) | **0 submits** |

Two independent proofs that the clock is genuinely a wall clock and not the caller's frozen `now`:

1. **Unpatched run.** With no clock patch at all, an otherwise perfect, AUTO-reserved, fully bound proposal anchored at `DEFAULT_NOW` yields **0 submits** and `NOT_SUBMITTED`, because `DEFAULT_NOW` is not the real instant. Under the old code this submitted.
2. **Real elapsed time.** With the boundary clock offset from a real monotonic source, injecting a genuine `time.sleep(16.0)` into the last pre-submit window blocks the submit; removing the sleep lets it through. The probe records the clock read and asserts it lands **between** the intent validation and `submit_order`.

The previous review's two structural probes — `test_the_submit_boundary_reads_the_kill_switch_but_not_the_clock` and `test_the_whole_execution_path_never_reads_a_wall_clock` — encoded the defect and now fail. They are **replaced**, not preserved, by semantic assertions that `_submit_leg` calls both `_utc_now` and `validate_canonical_quote`, that `_utc_now()` is within 2 s of the real clock and tz-aware, and that the clock read is immediately followed by a revalidation.

**Failure classification.** Every pre-mutation failure was proven `NOT_SUBMITTED`: `result.state is ExecutionState.NOT_SUBMITTED`, `submitted is False`, `blocked is True`, a specific non-empty `block_reason`, exactly one execution row in state `NOT_SUBMITTED`, and `load_unknown_orders() == ()`. Never `UNKNOWN`.

## 2. P1-2 — MANDATORY CANONICAL BINDING — **CLOSED**

`_persist_submission_intents` now refuses `price_bindings=None` (`canonical price bindings are required; fail closed`) and `{}` (`… are empty; fail closed`) before anything else, and `_submit_leg` independently refuses a missing or wrong-symbol binding at the mutation boundary.

Every probe below drives the **real production class** `opaca.broker.paper_execution.AlpacaPaperExecutionGateway`, constructed over a shape-compatible in-process stub client, so `verify_paper_client`, `require_paper_endpoint`, `assert_paper_execution_gateway`, the `__slots__` narrowing and the real `LimitOrderRequest` construction all execute for real. No network, no credentials.

| attack (real gateway) | broker submits |
| --- | --- |
| `price_bindings=None` | **0** |
| empty bindings `{}` | **0** |
| incomplete bindings (2-leg proposal, 1 binding) | **0** |
| binding for the wrong symbol | **0** |
| binding with the wrong quantity | **0** |
| mismatched canonical quote (valid binding, different print) | **0** |
| **invented matched $0.01 caller pair, BUY 100,000 SGOV** | **0** |
| **invented matched $0.01 caller pair, SELL 100 SGOV** | **0** |
| valid canonical binding | **1** — DAY LIMIT, `limit_price` 100.80, qty 3, side buy |

### UNBOUND RESERVATION AUTO IS NOT EXECUTION AUTHORITY — proven

`evaluate_and_reserve` still supports the offline/unbound reservation mode: an honest unbound proposal (`prices={SGOV: 100.69}`, `reference_price=100.69`) still reaches `is_auto=True`, and so does the invented `$0.01` pair. The brief permits this **only if** every real mutating path independently requires a complete canonical binding. That is what the retest measures:

* the honest unbound AUTO reservation handed to `execute_reserved_proposal` against the real gateway → **0 submits**, `blocked`, reason `canonical price bindings are required; fail closed`, **zero execution rows**;
* replayed a second time → still **0 submits**;
* the invented `$0.01` AUTO reservation → **0 submits**, zero execution rows;
* the same refusal on the offline `FakePaperExecutionGateway` path, so the behaviour is not gateway-specific.

Structurally: `submit_order` has exactly **one** call site, inside `_submit_leg`; `_submit_leg` has exactly one caller; and that call passes `price_bindings` as a keyword. There is no route to `submit_order` that skips the binding requirement.

## 3. P2-1 — EXACT PAPER ENDPOINT — **CLOSED**

All four production guards now delegate to `require_paper_endpoint`, which parses the URL and requires scheme `https`, host exactly `paper-api.alpaca.markets`, **no** port, **no** userinfo, **no** query, **no** fragment, and path in `{"", "/"}`. `is_live_endpoint` matches the live host exactly.

**265 probes**: 53 hostile endpoint forms × 5 guards (`require_paper_endpoint`, `broker.paper.verify_paper_client`, `execution.gateway.assert_paper_execution_gateway`, `execution.service._forbid_live_endpoint`, `preflight._verify_paper_endpoint`), plus the accept set and end-to-end checks. **All rejected with `PaperEnvironmentError`:**

`https://paper-api.alpaca.markets.evil.com` · `…markets.evil.com/v2` · `https://evil-paper-api.alpaca.markets` · `…markets.co` · `https://xpaper-api.alpaca.markets` · `https://paper-api-alpaca.markets` · `https://sub.paper-api.alpaca.markets` · trailing-dot and double-dot hosts · `https://api.alpaca.markets` (+`/v2`, `http://`) · `http://paper-api.alpaca.markets` · `ftp://` · scheme-relative and bare host · `https:/…` · `localhost`, `localhost:8080`, `127.0.0.1`, `127.0.0.1:443`, `0.0.0.0`, `[::1]` · `https://paper-api.alpaca.markets@evil.com` · `https://evil.com@paper-api.alpaca.markets` · `https://user:pass@paper-api.alpaca.markets` · `…@paper-api.alpaca.markets.evil.com` · ports `:443`, `:8443`, `:0`, `:` · `/v2`, `/../evil`, `?x=1`, `#evil.com`, `/@evil.com`, backslash `\@evil.com` and `\.evil.com` · empty, whitespace, leading/trailing space, `\n`, `\t`, `https://`, `://host`, `https:///host`, `https://:443`.

Accepted: `https://paper-api.alpaca.markets`, the same with a trailing `/` (normalised back to the canonical constant), and the case-insensitive host form.

End-to-end, `execute_reserved_proposal` raises `PaperEnvironmentError` before creating any execution row for the look-alike, the live host, the `http://` form and a wrong port; the real `AlpacaPaperExecutionGateway` cannot be constructed on any of them; and `paper=True` on the client does **not** rescue a bad base URL.

**No string-prefix acceptance survives.** An AST sweep of all 49 production modules finds **zero** `startswith` calls anywhere near an endpoint, base URL or `alpaca.markets` constant. The preserved Phase 3 test that asserted the look-alike *was* acceptable (`test_s18_only_the_paper_endpoint_prefix_is_accepted[…evil.com]`) now fails — correctly — and the builder removed its in-tree copy and added `tests/test_paper_endpoint.py`.

## 4. P2-2 — MARKET ADAPTER TEST GATE / PYTZ — **CLOSED**

* `pytz==2025.2` is pinned in **`requirements-dev.txt`** and in the **`paper` extra** of `pyproject.toml`; `alpaca-py==0.33.0` is pinned in both.
* The `pytest.importorskip("alpaca.data.requests")` autouse fixture on `TestAlpacaAdapterFailClosed` is **removed**. An AST sweep of the whole `tests/` tree finds **zero** `importorskip` calls and exactly **three** `pytest.mark.skip` declarations, all in `tests/conftest.py`, all live opt-ins.
* The canonical IEX adapter test and the SDK payload adapter tests execute: `test_iex_feed_is_requested` asserts on the captured request object that `feed is DataFeed.IEX` and `symbol_or_symbols == "SGOV"`.
* **Measured both ways.** In an environment built from `requirements-dev.txt` minus `pytz` (note: `alpaca-py==0.33.0` does *not* pull `pytz` transitively, so the pin is load-bearing):
  * at `11d1cde` → 5 **silent skips** in `test_market_price.py`;
  * at `193d7a2` → the **same 5 tests fail loudly** (`5 failed, 439 passed, 3 skipped`): `test_zero_price_rejected`, `test_negative_price_rejected`, `test_nan_price_rejected`, `test_fresh_sdk_trade_accepted`, `test_iex_feed_is_requested`.
* A normal offline suite in the full gate environment has exactly the three explicit live opt-in skips.

## 5. CRITICAL REGRESSION — **PASS**

Spot-checked directly against the real gateway (or the production offline double where fault injection is required), all under the new contract:

| control | verdict |
| --- | --- |
| duplicate-order prevention | **PASS** — five sequential retries → 1 submit, 1 execution row |
| concurrent executors | **PASS** — four threads on separate connections → 1 submit, 1 row |
| deterministic `client_order_id` / reservation↔broker correlation | **PASS** — `opaca-` + digest, reused, matched at broker, store and reservation |
| timeout / lost ACK → UNKNOWN | **PASS** — `UNKNOWN_REQUIRES_RECONCILIATION`, 1 submit |
| repeated UNKNOWN recovery | **PASS** — three recoveries, still UNKNOWN, still 1 submit, never re-minted |
| final kill switch | **PASS** — 0 submits |
| BUY reservation | **PASS** — reserved cash exactly `3 × 100.80`, strictly above `3 × 100.69`; equals `leg.notional` |
| SELL reservation / oversell | **PASS** — 60 of 100 submitted, a second 60 refused (`is_auto False`) |
| partial fills | **PASS** — filled 1, remaining 3, `PARTIALLY_FILLED` |
| bounded DAY LIMIT | **PASS** — `type=limit`, `time_in_force=day`, limit 100.80 > canonical 100.69 |
| max exposure = qty × LIMIT | **PASS** |
| paper-only mutation surface | **PASS** — 2 call sites; a live-endpoint gateway raises with 0 submits |
| read-only preflight | **PASS** — `preflight.py` names no mutator and imports no mutating gateway; refuses look-alike hosts; the preserved s1/s2/s7 suites run **99 passed** at this SHA |
| settlement idempotency, T+1 liquidity, multi-leg abort, approval hash/expiry, current-execution eligibility, stranded legs | **PASS** — carried green by the preserved suites under the adapted contract |
| zero bare asserts | **PASS** — 0 across 49 modules, green under `-O` and `-OO` |

### How the preserved suites were adapted, and what that does and does not prove

Run verbatim, the preserved `redteam/` tree shows 88 failures at this SHA. **70 of those are the new mandatory-binding call contract**: suites written before this commit call `execute_reserved_proposal` with no bindings and a frozen `now`, so they block before the broker and their assertions can no longer observe the behaviour they were written to attack (`assert 0 == 1`, `READY is FILLED`, and so on).

The adapter preserved as `redteam/closeout_193d7a2/contract_adapter.py`, applied to a **copy** of the tree, supplies only the new precondition — a canonical binding derived from the same `prices` mapping each test already passes (BUY at tolerance 0, exactly as the builder's own `bindings_for_proposal` does) and a boundary clock pinned to the test's own `now`. **No assertion was touched and no adversarial check was weakened.** With only that, the tree goes to **918 passed / 18 open markers**, stable across runs (occasionally 19 — the intermittent demo-DB race, P3-2).

The adapter is a regression harness and is explicitly **not** the basis of any P1 verdict, because it manufactures a "canonical" quote out of caller-supplied prices and pins the boundary clock — the two things P1-1 and P1-2 are about. Under it, `test_FINDING_no_freshness_revalidation_between_intent_and_submit_order` and `test_FINDING_execution_submits_a_limit_derived_from_an_invented_pair` still report, purely as artefacts of the harness; run **unshimmed** against the target both **pass** (0 submits), and the unshimmed closeout suite is what the P1 verdicts rest on.

The 18 remaining markers: **3 newly failing because the defect was fixed and the probe encoded the old behaviour** (`test_s18_only_the_paper_endpoint_prefix_is_accepted[…evil.com]`, `test_the_submit_boundary_reads_the_kill_switch_but_not_the_clock`, `test_the_whole_execution_path_never_reads_a_wall_clock`); **13 carried, unchanged from the baseline measurement at `11d1cde`** (5 inverted 79a7b1b-era probes, 6 standing backlog markers, the caller-pair reservation observation, the live-smoke frozen-`now` observation); and **1 intermittent** (the demo-DB overwrite race, which reproduced in 1 of 4 measured runs — unchanged, still P3-2). The baseline control run at `11d1cde` was **16 failed / 920 passed**, so the only genuinely new failures are the three intentional inversions.

---

## REMAINING P0

**None.**

## REMAINING EXECUTION-SAFETY P1

**None.** Both P1s from the previous review are closed and independently re-demonstrated.

## REMAINING FAIL-OPEN

No new fail-open path was found. Unchanged from the previous review:

* **P3-1** — a failed demo-DB initialization leaves an empty schema-v2 file on disk; the retry is then refused as "existing DB".
* **P3-2** — `init_paper_demo_store`'s overwrite refusal is a `Path.exists()` check outside the seeding transaction; reproduced in 1 of 4 runs this session. Seeding is idempotent, so it stays a contract/TOCTOU defect, not a data hazard.
* **P3-3** — `max_buy_cash_obligation` uses `round_budget` (ROUND_DOWN); cosmetic for whole shares, real only if fractional quantities are ever enabled.
* Six carried P2/P3 markers from earlier phases, all in `docs/backlog.md`: the bound-method `__self__` introspection escape; three store mutators outside transactions; a COMMIT failure leaving the connection in a transaction; leg-order-sensitive `proposal_hash`; the mutation scan excluding `spike/`; the non-hermetic test gate.

### New observations from this retest (none blocking)

* **`alpaca-py` coerces the limit price to a binary float at the SDK boundary.** `LimitOrderRequest(limit_price=format(Decimal("100.80"), "f"))` yields `limit_price = 100.8` as a Python `float`. Pre-existing, unchanged by this commit, and conservative in direction for a BUY (a float that rounds low lowers the cash bound), but money crosses the last boundary as a float. Worth a note in the backlog.
* **`_submit_leg` revalidates the freshness of whatever binding is in the map at that instant**, rather than re-running the full `price_binding_failure` check that the intent transaction ran. Swapping a *stale* binding in after the intent commit is caught (0 submits, proven); swapping a *fresh but differently priced* one is not re-checked — but the submitted order's limit comes from `leg.reference_price`, which is fixed and was fully bound in the intent transaction, so there is no economic effect. Defence-in-depth only.
* **The live mutation smoke still captures `now` before the quote fetch** and reuses it for the pre-boundary checks. That was fatal before this commit and is not any more, because the boundary re-reads the real clock — but the procedure would read more honestly if it re-fetched.
* **`evaluate_and_reserve` still returns `is_auto=True` for an unbound invented pair.** Permitted by the brief and harmless for execution (proven above), but an operator or a future caller reading `is_auto` from an offline reservation could mistake it for execution authority. The docstring already warns; consider naming the mode explicitly.

## PRE-LIVE BLOCKERS

**None.**

The standing non-code item from all three previous reviews is unchanged and is not a code defect: **nothing in this layer has met a real broker.** The adapters have never seen a live Alpaca fill payload or a live IEX trade payload, and `AlpacaPaperExecutionGateway` still detects a duplicate `client_order_id` by string-matching the exception text. `test_live_paper_readonly.py` and `python -m opaca preflight` should be run against real credentials — by a human, at a keyboard — before anything mutating is contemplated.

---

## RETURN

```
RED TEAM VERDICT:                     PASS
TARGET SHA:                           193d7a21cc956d2688f69e339cb79fc44cd34380

P1-1 FINAL PRICE FRESHNESS:           CLOSED
P1-2 REAL-MUTATION BINDING:           CLOSED
UNBOUND RESERVATION CAN REACH
  REAL SUBMIT:                        NO
P2-1 EXACT PAPER ENDPOINT:            CLOSED
P2-2 MARKET TEST GATE:                CLOSED

PRICE TOCTOU:                         PASS
0.01 REAL-MUTATION ATTACK:            BLOCKED
CANONICAL BINDING:                    PASS
BOUNDED BUY LIMIT:                    PASS
MAX CASH EXPOSURE:                    PASS
READ-ONLY PREFLIGHT:                  PASS
DUPLICATE ORDER SAFETY:               PASS
UNKNOWN RECOVERY:                     PASS

BUILDER TESTS:                        447 collected, 444 passed, 3 skipped,
                                      0 failed (+40 vs 11d1cde, none removed)
RED-TEAM TESTS:                       closeout retest suite 312/312 passed
                                      (3 deterministic runs); at 11d1cde the
                                      same suite fails 43/48 and one file will
                                      not import — the probes have teeth.
                                      Preserved redteam/ tree, contract-adapted:
                                      936 → 918 passed, 18 open markers
                                      (3 intentional inversions, 13 carried,
                                      1 intermittent P3-2). Baseline control at
                                      11d1cde: 16 failed / 920 passed.
OFFLINE SKIPS:                        exactly 3 — live paper smoke, live paper
                                      preflight, live paper mutation. Zero
                                      importorskip anywhere in tests/.
                                      Without pytz the 5 market-adapter tests
                                      now FAIL instead of skipping.

REMAINING P0:                         none
REMAINING EXECUTION-SAFETY P1:        none
REMAINING FAIL-OPEN:                  none new. Carried: P3-1 failed demo-DB
                                      init leaves a blocking file; P3-2 racy
                                      demo-DB overwrite refusal (1 of 4 runs);
                                      P3-3 max cash obligation rounds down;
                                      plus the six recorded backlog markers.
PRE-LIVE BLOCKERS:                    none

MERGE RECOMMENDATION:                 MERGE
FIRST PAPER TRADE READINESS:          READY FOR HUMAN PREFLIGHT
```

**On the merge recommendation.** All four blockers are closed, each proven by a probe that fails at the parent commit. The fixes are small, local and shaped like the controls already proven correct: the boundary now re-reads the clock exactly where the kill switch is re-read, and the binding requirement is enforced twice — once in the intent transaction, once independently at the mutation boundary. Nothing regressed: the builder suite grew 404 → 444 with no test removed, the mutation surface is still two call sites, every static gate and both scans are clean, and the preserved adversarial tree is green once given the new precondition it predates. The three preserved probes that now fail are probes that encoded the defects.

**First paper trade readiness is READY FOR HUMAN PREFLIGHT, not READY.** Everything reviewable offline has been reviewed. What remains is the standing item no test can close: a human running `python -m opaca preflight` and `pytest --live-paper` against real credentials, confirming `paper_account = ACTIVE` on the exact endpoint and that the IEX adapter parses a real trade payload, before any mutating run is contemplated.

**Not merged. No production code modified. No PAPER broker mutation performed. No credentials requested.**

---

*Artefacts preserved on `review/treasury-red-team`: this report and the 312-test closeout suite in `redteam/closeout_193d7a2/` (`closeout_support.py`, `test_r1_freshness_boundary.py` 14, `test_r2_binding_mandatory.py` 14, `test_r3_paper_endpoint.py` 265, `test_r4_market_gate.py` 5, `test_r5_regressions.py` 14), plus `contract_adapter.py` — the documented, opt-in regression harness used to re-read the pre-193d7a2 suites. Session-local and uncommitted: a byte-verified export of `193d7a21` and one of `11d1cde` for control runs, a `requirements-dev.txt`-pinned gate venv, and a second venv identical but for `pytz`.*
