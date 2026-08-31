# OPACA pre-live readiness — final red-team closeout @ 11d1cde

**RED TEAM VERDICT: PASS WITH FINDINGS**

**TARGET SHA:** `11d1cdeb4d283ba68264823e500ec14c58bf7324` — `feat: add bounded live-paper pricing and preflight`, tip of `origin/feat/prelive-readiness`
**BASELINE:** `da4a55ff5eb5d0f11cb5fbdcaec8a5f25aba21d9` (`origin/main`, tag `paper-execution-complete`) — verified as the target's **direct parent**; the branch is a single commit
**PREVIOUS RED-TEAM EVIDENCE:** `review/treasury-red-team @ 351af819a418d6ebb023ec7d1d2ef43019b0d405`

All coordinates resolve exactly. Reviewed from a byte-verified export of the target tree in a session-local scratch directory outside the repository — every one of the 114 tracked blobs hash-matches `git ls-tree 11d1cde`. The repository working tree was clean at start and end (`git status --porcelain` → 0), nothing was merged, no production code was modified, no finding was fixed, and **no PAPER broker mutation of any kind was performed.** No credentials were requested or used; the live smokes stayed skipped.

The diff is narrow: 31 files, +2222/−40, of which 16 are production modules. The new surface is `opaca/market/` (quote, source, limit, binding, errors), `opaca/preflight.py`, `opaca/persistence/demo.py`, `opaca/__main__.py`, and optional `price_bindings` parameters on `evaluate_and_reserve` / `execute_reserved_proposal`.

---

## GATES

| gate | result |
| --- | --- |
| builder suite | **404 passed / 3 skipped** (407 collected, 0 failed); 365/2 at `cd3dc86` → +42 tests, none removed |
| skips | the three live smokes only (`--live-paper`, `--live-paper-preflight`, `--live-paper-mutation`), all correctly gated |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 85 files already formatted |
| `mypy --strict` | Success: no issues found in **85** source files |
| `git diff --check` | clean, exit 0 (`da4a55f..11d1cde`) |
| credential scan | 114 tracked files → **0 literals**; only env-var *names* and redaction key lists |
| mutation capability scan | **2 call sites**, both `execution/service.py`, both on `mutate_gateway`; **0** HTTP/socket imports; 49 production modules |
| bare asserts (AST) | **0 `ast.Assert` nodes** across all 49 modules |
| `python -O` / `-OO` | 404 passed / 3 skipped under both |

**Toolchain:** the pinned gate environment (`mypy==2.3.1`, `pytest==9.1.1`, `ruff==0.16.5`) on CPython 3.11.15, plus `alpaca-py==0.33.0`.

## TEST INVENTORY

| suite | result |
| --- | --- |
| builder (`backend/tests`) | 407 → **404 passed**, 3 skipped, 0 failed |
| new pre-live red-team suite (this review) | 161 → **156 passed**, 5 findings |
| preserved Phase 3 suite at target | 143 → **138 passed**, 5 (3 inverted, 2 standing reports) |
| whole `redteam/` tree at target | 775 → **764 passed**, 11 (5 above + 6 carried P2/P3 markers) |

Deterministic across three consecutive runs, except one intermittent race described under P3-2.

**Teeth.** The whole new suite fails to even import at the baseline — `opaca.market` does not exist there — so the layer is genuinely new. The sharper evidence is the previous review's own probes: three of its five pre-live FINDING markers now **invert** at this SHA (`test_the_package_contains_no_market_data_client`, `..._prices_the_order_from_test_constants`, `..._a_wrong_price_changes_the_authority_outcome`), which is exactly what a real fix looks like.

---

## 1. CANONICAL LIVE PRICE SOURCE — **PASS**

`AlpacaPaperMarketData.get_latest_trade` is the sole price entry point. It builds `StockLatestTradeRequest(symbol_or_symbols=<sym>, feed=DataFeed.IEX)` — asserted directly on the captured request object — and is constructed only from `StockHistoricalDataClient`. AST scan of `market/source.py`: no `TradingClient`, no mutator name, no generic HTTP client anywhere in the 49 production modules.

* `__slots__ == ("_get_stock_latest_trade",)`; no `_client`, no `__dict__`, no callable mutator on the instance.
* Symbols outside `ASSET_UNIVERSE` are refused **before** any network call.
* No production module imports `tests` or references `DEFAULT_PRICES`. The only price constants left in the package are `Decimal("100.69")` inside `FakePaperExecutionGateway` (the offline double) and the demo opening-cash default.
* Every failure path raises. There is no branch anywhere that substitutes a fixture, a default, or a synthetic price: missing payload, missing symbol, `None` trade, SDK exception, timeout — all become `MarketDataUnavailableError`.
* `open_paper_market_data_from_env` fails closed with `PaperEnvironmentError` when credentials are absent.

Offline tests still use fixtures, as permitted.

## 2. PRICE VALIDATION / FRESHNESS — **PASS**

`DEFAULT_MAX_QUOTE_AGE_SECONDS == 15`. **58 attack cases, all fail closed.**

| attack | result |
| --- | --- |
| missing trade (`None`, `{}`, `{"SGOV": None}`, wrong symbol) | `MarketDataUnavailableError` |
| missing timestamp / missing price | refused |
| stale (60 s) | `StaleQuoteError` |
| **exact 15.000 s** | **accepted** — `age > max_age` semantics; a documented inclusive maximum, not a defect |
| 15 s + 1 µs | `StaleQuoteError` |
| future timestamp (+1 s, +1 day) | `FutureQuoteError` |
| naive `source_timestamp` / naive `fetched_at` / naive evaluation `now` | refused at construction or validation |
| non-UTC offset (+08:00), fresh and stale | compared correctly in both directions |
| malformed timestamps (`"not-a-timestamp"`, `"2026-13-45T99:99:99Z"`, `""`, `0`, `-1`, `1.5`, `[]`, `{}`, `"2026-09-01 14:29:59"`, naive `datetime`) | refused |
| zero, negative, `"nan"`, `"inf"`, `"Infinity"`, `""`, `"abc"`, `None`, `True`/`False`, `[]`, `{}`, arbitrary object | refused |
| float `nan`, `±inf`, `0.0`, `-1.0` | refused |
| `Decimal("NaN")`, `Decimal("Infinity")`, `Decimal("-Infinity")`, `0`, `-3`, `1e26`, `1e40` | refused |
| `max_age_seconds` of 0 or negative | refused — never treated as an infinite window |

A valid SDK binary float is converted through a fixed decimal string and is never used as a float in arithmetic (`100.69` → `Decimal("100.69000000")`).

## 3. PRICE TOCTOU — **FAIL**

This is the critical pre-live finding. Three of the four boundaries hold; the last one does not.

| window | required | measured |
| --- | --- | --- |
| stale before reservation | blocked | `is_auto=False`, `"quote age 60.0s exceeds max 15s; fail closed"`, no reservation |
| fresh at reserve, stale at the execute call | 0 submits | **0 submits**, blocked, reservation preserved |
| future-dated quote at execute | 0 submits | **0 submits**, blocked |
| kill switch flipped after reservation | 0 submits | **0 submits**, blocked |
| kill switch flipped **inside** the final pre-submit read | 0 submits | **0 submits**, blocked |
| **elapsed time between the last freshness check and `submit_order`** | **0 submits** | **1 submit** |

### P1-1 — no freshness re-validation at the final mutation boundary

`_persist_submission_intents` validates `validate_canonical_quote(bound.quote, now=now)` **inside** the intent transaction. `_submit_leg` — the actual mutation boundary — reads only `store.kill_switch_active()` and then calls `mutate_gateway.submit_order(...)`. There is no second freshness check.

Demonstrated by injecting 16.0 s of real elapsed time into the last pre-submit window (inside the conn-less `kill_switch_active` read, which sits between the committed intent and the broker call), with a spy recording every `validate_canonical_quote` call:

> the broker was mutated **16.0 s after the last quote-freshness validation**, exceeding the 15 s policy — `submit_calls == 1`, required 0.

Two structural facts make this unbounded rather than a lab artifact:

1. **`execution/service.py` never reads a wall clock.** AST scan: zero `datetime.now()` / `utcnow()` / `today()` calls. Freshness can only ever be measured against the caller's frozen `now`.
2. **The documented live procedure freezes `now` before the quote fetch.** In `test_live_paper_mutation.py`, `now = datetime.now(UTC)` is statement 13; the `latest_trades(...)` fetch is statement 14; the same `now` is then reused at statements 15, 21, 22, 24 and 26 for `validate_canonical_quote`, `evaluate_and_reserve` and `execute_reserved_proposal`. Every check therefore measures the quote against an instant that *precedes* the fetch.

Measured on the offline doubles, **13 broker read round-trips** occur between the quote fetch and `submit_order` (2 × `get_account`, 2 × `get_positions`, 6 × `get_asset`, 2 × `get_open_orders`, 1 × `get_order_by_client_id`), plus the three latest-trade calls and `get_clock` before them. Against a real endpoint with any retry or backoff, that sequence can exceed 15 s, and nothing measures it.

**Fix shape:** take a fresh `datetime.now(UTC)` inside `_submit_leg` and re-run `validate_canonical_quote` against the binding immediately before `submit_order`, returning `NOT_SUBMITTED` on failure — the same shape the kill switch already uses, which is proven correct in case C of the Phase 3 review.

## 4. CANONICAL PRICE BINDING — **$0.01 ATTACK: BLOCKED**; binding **sound but optional**

The original exploit is closed, and closed twice. `price_binding_failure` is now called unconditionally in **both** `evaluate_and_reserve` and `_persist_submission_intents`, and with `bindings=None` it still requires `prices[symbol] == leg.reference_price` for every leg.

| attack | result |
| --- | --- |
| 100 SGOV BUY with `reference_price=0.01` against `prices=100.69` | **blocked at reservation** — `"price binding mismatch"`, `decision is None`, zero cash reserved |
| the same leg handed straight to execution | **blocked**, `submit_calls == 0` |
| 300 SGOV understated at `10.00` (the old AUTO flip) | **blocked** — no longer promoted to AUTO; the honest 300-share order remains `APPROVAL_REQUIRED` |
| binding carrying a valuation ≠ the canonical print | `PriceBindingError` at construction |
| binding with an understated `max_cash_obligation`, a `reference_price` below the LIMIT, or a `limit_price` of 0.01 | `PriceBindingError` |
| binding for a different symbol / bindings for symbols not on the proposal | refused |
| missing policy price for a leg | refused |
| quantity changed after binding | refused |
| **lower price injected after reservation**, at the execute call | **blocked**, `submit_calls == 0`, reservation unchanged |
| **cheaper canonical quote swapped in at execution** | **blocked**, `submit_calls == 0` |
| reserved cash vs `qty × LIMIT` | exact: 3 × 100.80, strictly above 3 × 100.69 |

### P1-2 — the canonical binding is opt-in, not a precondition of mutation

`price_bindings` defaults to `None` on both `evaluate_and_reserve` and `execute_reserved_proposal`. With it omitted, the only requirement is that the two caller-supplied surfaces agree with each other. Nothing ties either to a market quote, and no freshness check runs at all.

Measured, with **no bindings**:

| proposal | prices | outcome |
| --- | --- | --- |
| BUY 100 SGOV @ 100.69 | honest | AUTO, notional 10,069.00 |
| BUY 300 SGOV @ 100.69 | honest | not AUTO (correct) |
| **BUY 100,000 SGOV @ 0.01** | 0.01 | **AUTO**, notional 1,000.00, cash reserved 1,000.00 |
| **BUY 1,000,000 SGOV @ 0.01** | 0.01 | **AUTO**, notional 10,000.00, cash reserved 10,000.00 |
| **SELL 100 SGOV @ 0.01** | 0.01 | **AUTO**, then submitted as a **DAY LIMIT at 0.01**, `submit_calls == 1` |

So the brief's requirement — *caller-controlled price cannot increase executable quantity* — holds on the bound path and **fails when bindings are omitted**: a matched invented pair raises the AUTO ceiling from ~200 shares to 1,000,000. The economic damage is contained by the LIMIT itself (a 0.01 BUY limit will not fill), but the SELL case is marketable and would liquidate the position while TreasuryGuard, the delegated-authority limits and the concentration check all reason about a $1 valuation. Quantity reservations still prevent an oversell.

**Fix shape:** make `price_bindings` mandatory whenever the mutating gateway is a real `AlpacaPaperExecutionGateway` (the service already type-checks the gateway at `assert_paper_execution_gateway`), or make it required outright and let offline tests pass explicit bindings.

## 5. BOUNDED BUY LIMIT — **PASS**; MAX CASH EXPOSURE — **PASS**

`DEFAULT_BUY_LIMIT_TOLERANCE == Decimal("0.001")` (10 bps), documented rather than magic. `buy_limit_price = round_money(canonical × (1 + tolerance), ROUND_UP)`, quantized to cents.

| canonical | ×1.001 | LIMIT |
| --- | --- | --- |
| 99.99 | 100.08999 | **100.09** |
| 100.00 | 100.10000 | **100.10** |
| 100.01 | 100.11001 | **100.12** |
| 100.69 | 100.79069 | **100.80** |
| 100.005 | 100.105005 | **100.11** |
| 100.695 | 100.795695 | **100.80** |
| 100.999 | 101.099999 | **101.10** |
| 0.01 | 0.01001 | **0.02** |
| 92.00 / 110.00 | — | 92.10 / 110.11 |

The limit is never below the canonical print across a swept grid of cent values; the exponent is always −2; a zero tolerance still rounds up to a valid cent (100.695 → 100.70); a negative tolerance fails closed; a non-positive or non-finite canonical price fails closed.

**Exposure.** `max_buy_cash_obligation = round_budget(qty × LIMIT)`, and `price_binding_failure` refuses the proposal unless that number equals `leg.notional`. Measured on a 5-share BUY against an account whose `buying_power` is 4 × cash: reserved cash **= 5 × 100.80**, ≠ 5 × 100.69, and the engine never reads `buying_power` at all (`policy/engine.py` states this explicitly and the grep confirms it — `buying_power` appears only in models, persistence and adapters).

**Submitted order.** Captured at the gateway boundary: `order_type == "limit"`, `time_in_force == "day"`, `side == BUY`, `limit_price == 100.80` — strictly above the canonical print. `PaperOrderRequest` refuses a market order carrying a limit, a limit order without one, any type other than market/limit, a non-positive limit, and any TIF other than `day`.

**SELL** is conservative: modeled price and limit both equal the canonical print, never a premium, and `max_cash_obligation` must be `None`.

## 6. FRESH SCHEMA-V2 DB — **PASS** (with two P3 items)

`SCHEMA_VERSION == 2`. A fresh `opaca-paper-demo.db`: schema v2, `journal_mode == WAL`, `foreign_keys` ON **and enforced** (a reservation referencing a non-existent proposal raises `IntegrityError`), `db_role == "paper-demo"`, exactly one `scenario_state` row, `execution_orders` / `approval_grants` / `system_state` present.

| requirement | result |
| --- | --- |
| refuse silent overwrite | **PASS** — `"refusing to overwrite existing DB … pass overwrite=True or delete the file explicitly"`; the file survives |
| explicit overwrite | replaces the DB and its `-wal`/`-shm` siblings |
| v1 DB fails closed | **PASS** — `PersistenceError: unsupported schema version 1`; no `ALTER TABLE` anywhere in `persistence/`; `open_existing_paper_demo_store` also refuses |
| wrong-role DB fails closed | **PASS** — a plain v2 store without the marker is refused for reuse |
| forbidden filenames / `:memory:` | refused (`opaca.sqlite`, `live-paper.sqlite`) |
| repeated startup does not reseed | **PASS** — three reopens return the identical seed, `COUNT(scenario_state) == 1` |
| concurrent initialization safe | **PASS** — four racing initializers agree on one seed, one `scenario_state` row, `PRAGMA integrity_check == ok` |
| failed initialization rolls back | **PARTIAL** — see P3-1 |

Migration is not required: a fresh dedicated demo DB is safe and explicit here.

## 7. READ-ONLY PREFLIGHT — **PASS** (with one P2)

`python -m opaca preflight` and `pytest --live-paper-preflight` both route to `run_read_only_preflight`.

* Wrapped the read gateway in a watchdog: the only calls made are `get_account`, `get_positions`, `get_asset`, `get_clock`, `get_calendar` — the permitted read set, plus the IEX latest trade and local demo-DB metadata.
* **No mutation of any kind.** AST scan of `preflight.py`: it never names `submit_order`, `cancel_order_by_id`, `evaluate_and_reserve`, `execute_reserved_proposal`, `persist_reservations`, `insert_execution_order` or `grant_human_approval`, never imports `PaperMutatingGateway` or `open_paper_execution_gateway_from_env`. Behaviourally, after a full preflight the demo DB has **zero execution orders, zero active reservations, and no persisted `preflight-sgov-buy-1` proposal**. The report always says `EXECUTION: NOT ATTEMPTED`.
* Live endpoint, empty endpoint, `localhost`, `https://evil.example.com`, `https://api.alpaca.markets/v2` → `paper_account = FAIL`, execution not attempted.
* Stale quote or unavailable market data → fails before the DB is created; the file does not exist afterwards.
* Refuses to silently overwrite an existing demo DB on a second run.
* **No credentials printed.** With `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` set to sentinels, neither value nor either variable name appears in `report.render()`.
* Without credentials the CLI prints `READ-ONLY PREFLIGHT: NOT RUN`, exits 2, and creates no database. `--help` advertises only `preflight`; any other command exits 2.
* The module docstring states in terms that a passing preflight is observational and is not execution authority, and that no stale preflight result may authorize a trade.

## 8. PHASE 3 EXECUTION REGRESSION — **PASS**

The preserved Phase 3 adversarial suite runs green against the exact target SHA: **138 of 143**, with the only failures being the pre-live readiness probes (three of which invert because they were written to assert the *old* broken behaviour). The whole 775-test `redteam/` tree shows **no execution-safety marker anywhere**.

Re-verified independently under the new LIMIT order path:

| control | verdict |
| --- | --- |
| duplicate-order prevention | **PASS** — five sequential retries → `submit_calls == 1`, one execution row |
| concurrent executors | **PASS** — four threads on separate connections → `submit_calls == 1`, one row |
| deterministic `client_order_id` | **PASS** — `opaca-` + sha256(proposal:leg)[:32], reused, never re-minted |
| lost ACK / timeout → UNKNOWN | **PASS** — holds `UNKNOWN_REQUIRES_RECONCILIATION`; three recoveries leave `submit_calls == 1` |
| final kill switch | **PASS** — 0 submits, blocked |
| SELL reservation / oversell | **PASS** — 60 of 100 filled, a second 60 is refused |
| BUY reservation | **PASS** — reserved at `qty × LIMIT`, released on fill |
| NOT_SUBMITTED multi-leg, partial fills, settlement idempotency, T+1 liquidity, approval hash/expiry, reservation↔broker correlation, current-execution eligibility | **PASS** — carried by the preserved suite, all green at this SHA |
| zero bare asserts | **PASS** — 0 across 49 modules, green under `-O` and `-OO` |
| paper-only mutation boundary | **PASS** — a live-endpoint gateway raises `PaperEnvironmentError` with `submit_calls == 0` |

No real broker mutation was performed at any point.

## 9. MUTATION SURFACE — **PASS** (with one P2)

Exactly **two** mutation call sites remain, both in `execution/service.py`, both on `mutate_gateway` (`submit_order` at line 565, `cancel_order_by_id` at line 316). `TradingClient` is imported only in `broker/alpaca.py` and `broker/paper_execution.py`; `LIVE_ENDPOINT` appears only in the four guards. The new market layer imports `alpaca.data.*` only — historical/read surfaces, no trading client, no raw mutable escape.

### P2-1 — the paper-endpoint guard is an unanchored prefix match

All four guards — `preflight._verify_paper_endpoint`, `execution.gateway.assert_paper_execution_gateway`, `execution.service._forbid_live_endpoint`, `broker.paper.verify_paper_client` — test `endpoint.startswith("https://paper-api.alpaca.markets")`. The attacker-controlled host `https://paper-api.alpaca.markets.evil.com` satisfies all of them: preflight reports `paper_account=ACTIVE`, `assert_paper_execution_gateway` accepts the gateway, and `verify_paper_client` accepts the client.

The previous review recorded this look-alike as *refused*; it is not. The preserved Phase 3 test `test_s18_only_the_paper_endpoint_prefix_is_accepted` in fact asserts that it **is** accepted, so the suite has been encoding the weakness rather than catching it.

Exploitability is low in the normal path — the base URL comes from a `TradingClient` constructed with `paper=True` and the `_paper` flag is checked separately — but this is the last structural line between the system and real money, and a prefix test is the wrong instrument for it. Anchor on the exact host (`urlsplit(url).netloc == "paper-api.alpaca.markets"`).

## 10. QUALITY GATES — **PASS** (with one gate P2)

All gates in the table at the top are green. One gate weakness:

### P2-2 — five market-data adapter tests skip silently on a missing transitive dependency

In an environment built from `requirements-dev.txt` plus `alpaca-py==0.33.0`, the builder suite reports **8 skipped**, not 3: `tests/test_market_price.py` skips five tests with `could not import 'alpaca.data.requests': No module named 'pytz'`. Those are precisely the tests that pin the IEX feed and the SDK payload adapter — the heart of section 1. Installing `pytz` brings the count to the expected 3 skips and all five pass.

A `pytest.importorskip` on an optional transitive dependency means the canonical-price adapter tests can vanish from a green run without anyone noticing. Pin `pytz` in `requirements-dev.txt`, or make those tests fail rather than skip when the paper extra is expected to be installed. This belongs alongside the already-recorded "non-hermetic test gate" backlog item.

---

## REMAINING P0

**None.**

## REMAINING EXECUTION-SAFETY P1

**Two, both new in this branch, both pre-live blockers.**

* **P1-1 — no quote-freshness re-validation at the final mutation boundary.** `_submit_leg` reads only the kill switch. Elapsed wall time between the last freshness check and `submit_order` is unmeasured and unbounded; the live procedure freezes `now` before the quote fetch and then makes 13 broker round-trips. Demonstrated: 16.0 s of drift, `submit_calls == 1`, required 0.
* **P1-2 — the canonical binding is optional.** `price_bindings=None` reduces the guard to "the caller's two numbers agree with each other". A matched invented pair at $0.01 reaches AUTO for 1,000,000 SGOV and is submitted as a DAY LIMIT at the invented price.

## REMAINING FAIL-OPEN

* **P2-1** — unanchored `startswith` paper-endpoint guard accepts `paper-api.alpaca.markets.evil.com` (four call sites).
* **P2-2** — market-data adapter tests skip silently when `pytz` is absent.
* **P3-1** — a failed demo-DB initialization rolls back the *transaction* (no scenario, no `db_role`) but leaves an empty 212 KiB schema-v2 file on disk; the retry is then refused as "existing DB", steering the operator toward the destructive `--overwrite-db`. Clean up the file when bootstrap fails after creating it.
* **P3-2** — `init_paper_demo_store`'s overwrite refusal is a `Path.exists()` check outside the seeding transaction. Racing initializers on one fresh path can all be handed a "fresh" store (reproduced in 2 of 8 runs). Seeding is idempotent (`CHECK (id = 1)`, `seed_scenario_once` under `BEGIN IMMEDIATE`), so no data is duplicated and integrity_check stays clean — a contract/TOCTOU defect, not a data hazard.
* **P3-3** — `max_buy_cash_obligation` uses `round_budget` (ROUND_DOWN). A stated *maximum* obligation should round up. Understatement is under one cent per leg and exactly zero for whole shares, so it is cosmetic today; it becomes real if fractional quantities are ever enabled.

Six carried P2/P3 markers from earlier phases remain, unchanged and all recorded in `docs/backlog.md`: the bound-method `__self__` introspection escape; three store mutators outside transactions; a COMMIT failure leaving the connection in a transaction; leg-order-sensitive `proposal_hash`; the mutation scan excluding `spike/`; the non-hermetic test gate.

## PRE-LIVE BLOCKERS

1. **P1-1 — freshness at the mutation boundary.** Re-read the clock and re-validate the binding inside `_submit_leg`, immediately before `submit_order`, returning `NOT_SUBMITTED` on failure. Until then the 15-second policy is enforced only against an instant the caller chose, and the first real order can be priced off a quote of any age.
2. **P1-2 — mandatory bindings on the mutating path.** An optional guard on the last hop before real orders is not a guard. Require `price_bindings` whenever the gateway is a real paper execution gateway.
3. **P2-1 — anchor the endpoint check** on the exact host, in all four places, and fix the Phase 3 test that currently asserts the look-alike is acceptable.

And the standing item from the previous two reviews, unchanged: **nothing in this layer has met a real broker.** The adapters have never seen a live Alpaca fill payload or a live IEX trade payload, and `AlpacaPaperExecutionGateway` still detects a duplicate `client_order_id` by string-matching the exception text. `test_live_paper_readonly.py` and the read-only preflight should be run against real credentials — by a human, at a keyboard — before anything mutating is contemplated.

---

## RETURN

```
RED TEAM VERDICT:                PASS WITH FINDINGS
TARGET SHA:                      11d1cdeb4d283ba68264823e500ec14c58bf7324
CANONICAL PRICE SOURCE:          PASS
PRICE FRESHNESS:                 PASS
PRICE TOCTOU:                    FAIL
0.01 REFERENCE-PRICE ATTACK:     BLOCKED
CANONICAL PRICE BINDING:         PASS on the bound path; FAIL as a precondition
                                 (price_bindings is optional — P1-2)
BOUNDED BUY LIMIT:               PASS
MAX CASH EXPOSURE:               PASS
FRESH SCHEMA-V2 DB:              PASS
READ-ONLY PREFLIGHT:             PASS
PHASE 3 EXECUTION REGRESSION:    PASS
DUPLICATE ORDER SAFETY:          PASS
UNKNOWN RECOVERY:                PASS
PAPER-ONLY BOUNDARY:             PASS (endpoint prefix unanchored — P2-1)

BUILDER TESTS:                   407 collected, 404 passed, 3 skipped, 0 failed
RED-TEAM TESTS:                  new suite 161 (156 passed, 5 findings)
                                 Phase 3 preserved 143 (138 passed, 3 inverted,
                                 2 standing reports)
                                 whole redteam/ tree 775 (764 passed, 11)

REMAINING P0:                    none
REMAINING EXECUTION-SAFETY P1:   P1-1 no freshness re-check at the mutation
                                 boundary; P1-2 canonical binding is optional
REMAINING FAIL-OPEN:             P2-1 unanchored paper-endpoint prefix;
                                 P2-2 market-data tests skip on missing pytz;
                                 P3-1 failed demo-DB init leaves a blocking file;
                                 P3-2 racy demo-DB overwrite refusal;
                                 P3-3 max cash obligation rounds down
PRE-LIVE BLOCKERS:               P1-1, P1-2, P2-1

MERGE RECOMMENDATION:            FIX THEN RETEST
FIRST PAPER TRADE READINESS:     NOT READY
```

**On the merge recommendation.** This branch is a large, well-shaped improvement and it regresses nothing: the builder suite grew 365 → 404 with no test removed, the mutation surface is still two call sites, all static gates and both scans are clean, and three of the previous review's five pre-live findings invert here. `BoundExecutionPrice` is a real invariant object rather than a flag, and the binding check runs in both transactions rather than once. On the merits of the diff alone this would be a MERGE.

It is **FIX THEN RETEST** because this was billed as the *final* gate before real orders, and the brief's own criterion for section 3 — *if quote age exceeds policy at the submit boundary, broker submit count must be 0* — is not met, in the very code written to close live pricing. The two P1 fixes are small and local, and both have a proven shape already in the codebase (the kill switch is re-read at exactly the boundary P1-1 needs). Closing them and re-running this suite is a short loop; merging first and closing them after leaves an opt-in guard and an unmeasured clock inherited by the next phase.

**Not merged. No production code modified. No PAPER broker mutation performed. No credentials requested.**

---

*Artefacts (session-local, outside the repository, uncommitted): a byte-verified export of `11d1cde` plus exports of `da4a55f` and `cd3dc86`; a pinned 3.11.15 gate venv; and a new 161-test pre-live red-team suite — `test_s1_canonical_source.py`, `test_s2_freshness.py`, `test_s3_toctou.py`, `test_s4_binding.py`, `test_s5_bounded_limit.py`, `test_s6_demo_db.py`, `test_s7_preflight.py`, `test_s9_mutation_surface.py` — driven by the same `OPACA_BACKEND` harness convention as the preserved suites.*
