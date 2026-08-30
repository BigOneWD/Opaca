# OPACA Phase 3 red-team review — paper execution lifecycle

**RED TEAM VERDICT: PASS WITH FINDINGS**

**TARGET SHA:** `79a7b1b837c86dc700533eeda6f5699b197a7d4a` (`feat: implement safe Alpaca paper execution lifecycle`)
**BASELINE:** `main @ 12f4eb02f9c832f7368cc0c06f67afaf4bb1d7d8` — verified as the target's parent, and confirmed to contain Phase 2 (`624439f`)
**PHASE 2 EVIDENCE:** `review/treasury-red-team @ a129a8375091e65d2027364949c6f30224797310`

All three coordinates resolve exactly. Reviewed in an isolated detached worktree at the target SHA; worktree clean at start and end. No production code modified, nothing merged, nothing committed.

**Scope of this review, as agreed:** full architecture red-team, **offline only**. No credentials were requested, no live call was made, and **no mutation of any kind was performed** — including against the paper endpoint. Every probe runs against the production doubles and an isolated SQLite store.

Toolchain unchanged and rebuilt to the pins in `backend/requirements-dev.txt`: CPython 3.11.15 · pytest 9.1.1 · ruff 0.16.5 · mypy 2.3.1 (compiled).

---

## BUILDER GATES

| gate | result |
| --- | --- |
| full builder suite | **344 passed / 2 skipped** (309 at `624439f`) |
| skips | `test_live_paper_readonly.py` and `test_live_paper_mutation.py`, both correctly gated |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 68 files already formatted |
| `mypy --strict` | Success: no issues found in 68 source files |
| `git diff --check` | clean, exit 0 (worktree, and `12f4eb0..79a7b1b`) |
| credential scan | detect-secrets 1.5.0 over 97 tracked files → **1 finding, the same false positive** (`paper.py:11`, the literal env-var *name*). Regex sweep → 0. |
| mutation capability scan | see below |
| `python -O` / `-OO` | 344 passed / 2 skipped under both |

### Mutation capability scan

An AST scan of all 33 modules in `backend/opaca/` finds **exactly two mutation call sites**, and both are in the one module allowed to have them:

```
execution/service.py:292   mutate_gateway.cancel_order_by_id()
execution/service.py:510   mutate_gateway.submit_order()
```

Both are invoked on the injected `mutate_gateway`, never on a client. **Zero** imports of `requests`, `httpx`, `urllib`, `http`, `aiohttp`, `socket` or `websockets` anywhere in the package. The read-only modules (`broker/gateway.py`, `broker/alpaca.py`, `broker/adapters.py`, `reconciliation/service.py`, `orchestration/*`, `policy/engine.py`) contain no mutation call at all.

This is the cleanest possible answer to the question this phase raises. The blast radius of "can this thing place an order" is two lines.

---

## RED-TEAM TESTS

**94 passed / 7 findings** in the new Phase 3 suite, deterministic across three runs. `-k "not FINDING"` → 94 passed.

Phase 2 + treasury-core regression against this branch: the full `redteam/` tree is **719 passed / 14 failed** across 733 tests. Of the 14, seven are the Phase 3 findings and seven are markers in the older suites — six carried, one a genuine regression (P2-1 below).

**No Phase 2 or Treasury Core safety control regressed.** Five older probes needed retargeting because the phase boundary legitimately moved, and each was re-pointed at the invariant rather than relaxed:

* the "no mutation anywhere in the package" scan became "mutation is confined to `execution/service.py`, called only on `mutate_gateway`, and limited to `submit_order`/`cancel_order_by_id`" — asserted on call sites and receivers, not name mentions, and additionally asserting the read-only modules stay clean;
* the dynamic-dispatch scan's semantic allow-list gained the Phase 3 guard function (same pattern as the Phase 2 guard: it walks the forbidden-name set in order to *refuse* a gateway);
* two schema assertions pinned to literal `1` now compare against `SCHEMA_VERSION` (the schema legitimately moved to 2);
* two Phase 2 **findings are now closed** and were inverted — see below.

### Phase 2 findings closed by this phase

* **Reservations are never released** (Phase 2 P2-1) — **CLOSED.** `execution/reservations.py` resizes against proven fills and releases on a proven terminal state, and `ReservationStatus.RELEASED` is now used.
* **One AUTO sell permanently locks out every later buy of that symbol** (Phase 2 P2-2) — **CLOSED.** Verified end to end: an opposing BUY is REJECTed while the sell reservation is live, and is AUTO again once the sell fills and its reservation is released. This was the most operationally consequential item in the Phase 2 backlog.

---

## WHAT HOLDS

### Submission safety — the core question of this phase

Thirty-five probes, all green. At most one broker order per logical leg in every attack I could construct:

* **Retry** — five repeats of `execute_reserved_proposal` after a successful submit: `submit_calls == 1`, one execution row, every repeat returns `recovered=True, submitted=False`.
* **Concurrency** — four threads on separate connections racing the same proposal: **one** submit. The duplicate check is inside `BEGIN IMMEDIATE`, and the `execution_orders` primary key backstops it.
* **Lost response, both sides of the accept** — `timeout_before_accept` and `timeout_after_accept` both land in `UNKNOWN_REQUIRES_RECONCILIATION` with `submit_calls == 1`; three further retries never mint a second order. Capacity is retained: *a timeout is not a release.*
* **Crash between intent and broker call** — a `KeyboardInterrupt` at the submit boundary leaves the intent row committed as `SUBMITTING`; restart recovery looks the order up and never resubmits, and the reservation is retained.
* **Broker reports a duplicate `client_order_id`** — looked up, never re-minted, audited as `ORDER_RECOVERED`.
* **Recovery finds a landed order** — the order that was accepted before the timeout is recovered as `FILLED`, still `submit_calls == 1`, and only then is capacity released.

The design that makes this work is the right one: the submission intent is committed inside a transaction *before* any broker I/O, and the broker I/O happens outside it. Uncertainty is a state, not an error.

### Pre-submission gates

Every one blocks with **zero broker calls**: kill switch; unreserved proposal; payload mutated after reservation; `DRIFT_DETECTED`; broker unavailable; a policy tightened between reserve and execute; `environment_verified=False`; approval required and not granted; approval granted but hash-mismatched; approval expired. Revalidation is always audited, and `EXECUTION_REVALIDATED` always precedes `SUBMISSION_INTENT_CREATED`.

Human approval is properly bound: a grant is tied to the proposal *and* its payload hash, cannot be created for a non-existent or expired proposal, is idempotent, and **never overrides a hard REJECT** — a granted proposal whose symbol is later de-whitelisted is blocked with `submit_calls == 0`.

### Paper-endpoint boundary

`https://api.alpaca.markets`, an empty endpoint, `localhost`, an arbitrary host, and the look-alike `https://paper-api.alpaca.markets.evil.com` are all refused before any call. Ten extra mutator names planted on the execution gateway are each rejected, as is a nested mutable client under `_client`. A read gateway carrying `submit_order` is refused by `assert_read_only_gateway` before execution begins.

### Reservation accounting

Full fill releases; partial fill resizes to exactly the remainder (checked at 1, 9 and one-billionth of a share); zero fill retains the whole reservation; rejection releases; cancel-after-partial-fill releases only the dead remainder and does not resurrect the filled shares; an `UNKNOWN` leg blocks release for *every* leg of its proposal; buy-side cash reservations release and resize the same way; released rows are marked, not deleted; repeated recovery is idempotent. Quantities and amounts never go negative.

**The oversell attack fails**: sell 60 of 100, fill, release — a second 60-share sell is refused and a 40-share sell is authorised and fills to zero. After a partial fill, a proposal for one share more than the true free quantity is refused.

### State machine, recovery, audit

Terminal states are absorbing; nothing transitions backwards into `READY` or `SUBMITTING`; `PARTIALLY_FILLED` cannot regress to `SUBMITTED`; every open state can reach `UNKNOWN`; unmapped broker statuses map to `UNKNOWN_REQUIRES_RECONCILIATION`, never to a terminal state. `UNKNOWN` cannot be cancelled until recovered, and an unavailable lookup or an order absent at the broker both keep it `UNKNOWN` with capacity retained. Restart recovery covers every open proposal.

A filled order leaves a complete trail: `EXECUTION_REVALIDATED → SUBMISSION_INTENT_CREATED → ORDER_SUBMITTED → ORDER_ACKNOWLEDGED → FULL_FILL → SETTLEMENT_CREATED → RESERVATION_RELEASED`, carrying the `client_order_id` and containing no credential-shaped text. Repeated recovery does not duplicate settlement proceeds, and proceeds never exceed the filled notional. Five corrupt broker acknowledgements (`filled > qty`, NaN, negative, malformed quantity, malformed side) never produce a clean fill.

### Live paper mutation test

Double-gated behind `--live-paper-mutation` *and* credential presence, skipped by default, and it submits at most BUY 1 SGOV. Correctly built. **Not run.**

---

## FINDINGS

### P1-1 — a zero net position delta is never compared against local fills

Phase 3 relaxes a Phase 2 control: `compare_state` previously flagged *any* position change versus the prior snapshot as `DRIFT_DETECTED`; it now accepts a delta that exactly matches locally recorded, not-yet-stamped fills. The relaxation is well bounded in every direction I tested — an external change on top of our fill, an external change in the opposite direction, a purely external change, a disappearing symbol, and re-use of an already-stamped fill are all still `DRIFT_DETECTED`.

But the loop short-circuits before the comparison:

```python
delta = curr_qty.get(symbol, ZERO) - prev_qty.get(symbol, ZERO)
if delta == ZERO:
    continue                      # explained[symbol] is never consulted
if delta != explained.get(symbol, ZERO):
    unexplained = True
```

So when the broker position is unchanged but local records say it should have moved, nothing is flagged. Reproduced twice: as a unit (a recorded 10-share SELL fill against an unchanged position — `explained = −10`, `delta = 0` — returns `RECONCILED`), and end to end (our 10-share sell fills, an external +10 arrives before we reconcile, the net zero delta reconciles clean and the external movement is never surfaced).

Two ways to reach it: a busted or reversed trade that we have already recorded as filled, or an external movement that exactly offsets ours. Neither causes an oversell — the position is genuinely there — and a phantom fill would create unsettled proceeds that the cash-consistency check would eventually catch. But this is the position-drift detector, and it has a blind spot at exactly the value where "nothing changed" and "two things cancelled out" are indistinguishable. **Fix: drop the `delta == ZERO` short-circuit and compare `delta != explained` for every symbol.** One line.

### P1-2 — every live order is counted twice against its own capacity

The resized `SELL_QUANTITY` reservation and the broker order that reservation produced are the *same* encumbrance, but `build_policy_context` feeds both into `CHECK-16`:

```
Sources: [('broker:opaca-18df7fca…', '40'), ('s1', '40')]   → counted 80 against a real 40
```

`_sell_unresolved_from_reservations` emits one synthetic unresolved order per active reservation, and `_unresolved_from_broker_orders` emits another for the same order once a reconcile has captured it into the snapshot. `sell_reservations` sums both.

The general case is starker than the partial-fill case: **one live 10-share sell consumes 20 of capacity**, so an honest 90-share sell against the 90 untouched shares is `REJECT`ed. After a partial fill of 20 on a 60-share sell — 80 shares held, 40 genuinely encumbered — no further sell of any size can be authorised.

This is **fail-closed**: `effective_available = min(broker available, quantity − reserved)`, and double-counting only lowers the bound. It cannot oversell. But it violates the invariant `CHECK-16`'s own docstring promises — *"never a double subtraction of the reservation"* — and it is an availability defect that bites on every open order, which is precisely the state this phase introduces. The Phase 2 suite could not see it because Phase 2 had no orders of its own at the broker. **Fix: skip a broker order whose `client_order_id` matches an active reservation, exactly as `compare_state` already does for the hold-aside calculation.**

### P1-3 — a rejected first leg strands the remaining legs

Intents for *all* legs are committed before the first submit. `execute_reserved_proposal` then breaks out of the leg loop on `REJECTED` or `UNKNOWN`. On a rejection, leg 0 is `REJECTED` and leg 1 is left in `SUBMITTING` with `broker_order_id = None` — an order that was never sent.

`sync_proposal_reservations` returns early while any leg is `SUBMITTING`, so **both legs' reservations are retained indefinitely**. Recovery cannot distinguish "never sent" from "sent and lost": `_sync_leg_from_broker` finds nothing at the broker and reclassifies leg 1 as `UNKNOWN_REQUIRES_RECONCILIATION`, which is a permanent state requiring human review for an order that does not exist. Verified end to end.

Fail-closed — nothing is submitted, no capacity is freed — but the proposal jams and only manual intervention clears it. **Fix: on a terminal break, mark the un-submitted legs from their own intent rows rather than leaving them to be discovered at the broker; a leg with no `broker_order_id` that was never dispatched is knowably `REJECTED`/cancelled, not `UNKNOWN`.**

### P2-1 — a bare `assert` reappeared in the production package

`backend/opaca/execution/service.py:184: assert last is not None`. This regresses a treasury-core control (P2-d) closed at `bc5fcda` and independently re-verified at `d85a2e6` and `624439f`: the package contained **zero** `assert` statements, so behaviour is identical under `python -O`.

Bounded honestly by the probe beside it: the guarded value only stays `None` when the leg loop never runs — a proposal with no legs — and such a proposal is refused by revalidation long before the assert, with `submit_calls == 0`. So this is a regression of a control and a hygiene defect, **not an exploitable one**. It should still be replaced with an explicit branch, because the whole point of the closed control was that nobody has to make this argument again.

### P3-1 — the kill switch is not re-read immediately before submission

The kill switch is checked inside the submission-intent transaction; the broker call happens outside it. A flip in that window is not seen — demonstrated by flipping it inside a patched `submit_order` and watching the order complete.

This window cannot be closed completely against a remote broker, and the check that exists is in the right place for atomicity. But a last-moment re-read costs one row and one comparison, and would shrink the window from "the whole revalidation-to-network span" to "the network call itself".

---

## REMAINING P0

**None.** No attack produced a duplicate order, an oversell, an over-release, a submission under a closed gate, or a live-endpoint call.

## REMAINING P1

Three, all fail-closed: **P1-1** (zero-delta drift blind spot), **P1-2** (double-counted live orders), **P1-3** (stranded legs after a rejection).

## REMAINING FAIL-OPEN SAFETY/AUTHORITY ISSUE

**None.** Every finding in this review errs toward refusing, retaining, or not seeing — never toward acting. The one detection gap (P1-1) hides information; it does not authorise anything.

## P2/P3 BACKLOG

New this phase: **P2-1** (bare `assert` regression), **P3-1** (kill-switch window).

Carried unchanged from Phase 2 and treasury-core, all re-verified as still open and all recorded in `docs/backlog.md`: the `__self__` introspection escape on retained bound methods (now applying to the execution gateway too); three store mutators running outside transactions; a COMMIT failure leaving the connection in a transaction; leg-order-sensitive `proposal_hash`; the mutation scan excluding `spike/`; the non-hermetic test gate.

`docs/backlog.md` has not yet been updated for the two items Phase 3 closed (reservation release, symbol lockout) — worth trimming so the backlog stays a live document.

---

## MERGE RECOMMENDATION: **FIX THEN RETEST**

This is a well-built phase. The hardest thing in it — never sending a second order, under retry, concurrency, timeouts on either side of the broker's accept, and a crash between intent and submission — holds under every attack I could construct, and the mutation surface is two lines. It also closes the two Phase 2 backlog items that mattered most.

Three things should be fixed before merge, and none is deep:

1. **P1-2** — the double-counted live order. This one is behavioural: as written, the system cannot authorise a normal-sized second order while any order is open, and the effect scales with order size. It is the difference between a layer that can trade daily and one that trades once per reconcile cycle.
2. **P1-1** — the `delta == ZERO` short-circuit. One line, and it restores a Phase 2 control to full strength.
3. **P1-3** — stranded legs after a rejected leg. Multi-leg proposals currently jam on any rejection.

P2-1 and P3-1 are small and can ride along or follow.

Two things for the phase after this one, both now the highest-risk untested assumptions:

* **Nothing here has been exercised against a real broker.** The adapters have never seen a live Alpaca fill payload, and `AlpacaPaperExecutionGateway.submit_order` detects a duplicate `client_order_id` by matching `"client_order_id" in message and "unique" in message` against the exception text. If Alpaca's wording differs, a duplicate becomes a generic `BrokerUnavailableError` — which is handled safely (it becomes `UNKNOWN` and never resubmits), but the *specific* recovery path would never fire. The read-only live smoke and then the double-gated mutation smoke should be run on the Mac mini, in that order, before this layer is trusted with a schedule.
* **The reservation lifecycle now exists but has never seen a real partial fill.** P1-2 is the first evidence that the interaction between reservations and live broker orders was not fully modelled; a live partial fill is where the rest of that interaction will show up.

**Not merged. No production code modified. Nothing committed.**

---

*Artefacts: a detached worktree at `79a7b1b` (session-local, outside the repository); a new 101-test Phase 3 probe suite at `redteam/paper_execution_79a7b1b/`; and retargeting edits to five probes in `redteam/reconciliation_3fdabf3/` and `redteam/closeout_bc5fcda/`. All of it is preserved on `review/treasury-red-team`. Stale `.lock` entries under `.git/` from earlier sessions still cannot be pruned — the mount denies unlink.*
