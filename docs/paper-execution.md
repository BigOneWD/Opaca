# Paper execution lifecycle

This phase sits on Treasury Core and the Phase 2 reconciliation/reservation
layer. It does **not** redesign those layers. Live money is impossible.

```text
evaluate → reserve → fresh recon → TreasuryGuard → authority
  → persist submission intent → submit → ack | UNKNOWN
  → fill sync → resize/release reservation → T+1 settlement → audit
```

## Safety contract

* AI reasons. Software enforces. Alpaca executes.
* Unknown ≠ failed. Uncertainty never creates a second order.
* Executor keys on **current** execution eligibility. `reserved == True` is
  not permission to submit. Historical AUTO is not currently executable AUTO.
* Immediately before submission: fresh broker reconciliation, latest snapshot,
  TreasuryGuard re-run, authority re-run, reservation validation. Any failure
  is STOP / REVIEW / UNKNOWN — not submit.
* Kill switch is re-checked inside the submission-intent transaction and
  immediately before `submit_order`. A block at that point is proven
  non-submission (`NOT_SUBMITTED`); it is not broker REJECTED and not UNKNOWN.

## Gateway boundary

Read path remains `ReadOnlyAlpacaGateway`. Mutation is a **separate** paper-only
gateway exposing only:

* `submit_order`
* `cancel_order_by_id` (known broker id only; UNKNOWN cannot cancel)

No raw `TradingClient`, no generic HTTP client, no live endpoint. Paper URL
is verified at construction and again before submit.

## Order identity

`client_order_id = opaca-` + sha256(`proposal_id:leg_index`)[:32] (38 chars,
Alpaca max 128). Retry of the same logical leg reuses the id. The execution
row is persisted **before** the broker call. After a lost response the only
legal action is lookup by `client_order_id`.

A local reservation and the broker open order for that same `client_order_id`
are one economic commitment and are counted once. External broker orders,
unresolved UNKNOWN size, and uncorrelated / ambiguous identities fail closed.

## State machine

`READY` (reserved, no execution row) → `SUBMITTING` → `SUBMITTED` →
`PARTIALLY_FILLED` / `FILLED` / `REJECTED` / `CANCEL_PENDING` / `CANCELLED` /
`NOT_SUBMITTED` / `UNKNOWN_REQUIRES_RECONCILIATION`.

Illegal transitions fail closed. Terminal states do not resubmit.
`UNKNOWN_REQUIRES_RECONCILIATION` cannot return to `SUBMITTING` or
`NOT_SUBMITTED`. If a later leg is never sent because an earlier leg stopped,
it becomes `NOT_SUBMITTED` (broker submit count 0). Uncertainty after a
submit attempt remains UNKNOWN.

## Reservation lifecycle

| Disposition | BUY cash reservation | SELL qty reservation |
| ----------- | -------------------- | -------------------- |
| Unsubmitted | retained | retained |
| UNKNOWN / SUBMITTING | retained (full) | retained (full) |
| Proven NOT_SUBMITTED | released | released |
| Partial fill | resized to remaining notional | resized to remaining qty |
| Full fill / reject / cancel | released | released |

A network timeout is not a release.

## Canonical market price

The live-paper path reads Alpaca IEX **latest quote**
(`alpaca.stock.latest_quote.iex`) for symbols economically required by the
current decision. BUY uses ask; SELL uses bid. Held permitted inventory
needed only for valuation is marked at the bid. Credentials stay in the
environment. The data client is separate from the mutating gateway.
Missing, non-positive, non-finite, future, crossed, or stale required quotes
fail closed. Unused whitelist symbols do not block. There is no fallback to
last trade, SIP, `tests.helpers.DEFAULT_PRICES`, or any synthetic price.

The hard freshness control, applied only to required symbols, is **fetch /
decision freshness** (`DEFAULT_MAX_QUOTE_FETCH_AGE_SECONDS` = 15):
`now - fetched_at` is how recently Opaca obtained the latest IEX quote from
Alpaca. Override per call; never silently extend. Inclusive: fetch age equal
to 15 seconds is accepted; one microsecond beyond is not.

**Source-event age** (`now - source_timestamp`) is diagnostic metadata for
IEX latest-BBO. A latest-quote request can be fetched now while the latest
IEX BBO event itself is minutes old if IEX has emitted no newer event. It
is recorded in the quote model, audit, preflight output, and execution
diagnostics. It is not a hard execution blocker. A future
`source_timestamp` remains invalid.

Immediately before `submit_order`, the production PAPER path fetches a **new**
IEX latest quote for the executable symbol. It does not re-check the old
quote object. The new observation must pass fetch freshness ≤ 15s and a
valid bid/ask/spread. BUY submits only if the final ASK is ≤ the already
approved BUY LIMIT; the LIMIT is never widened. SELL is symmetric and is
never made more aggressive than the approved bound. A final quote is an
additional safety check, not new authority.

`PolicyContext.prices[symbol]` and `leg.reference_price` are bound to one
`CanonicalMarketPrice`. A caller cannot inject TreasuryGuard `100` and
execution `reference_price=0.01`. Mismatch fails closed and cannot reach
executable AUTO.

## Bounded BUY LIMIT

Paper execution submits **DAY LIMIT** orders. A BUY limit is

```text
limit = round_up_cents(canonical × (1 + tolerance))
max cash = round_budget(qty × limit)
```

Default `tolerance` is **10 basis points** (`0.001`,
`DEFAULT_BUY_LIMIT_TOLERANCE`). It is an explicit configured argument, not a
hidden constant. TreasuryGuard evaluates BUY notional at that LIMIT (maximum
cash obligation). The broker order uses the same LIMIT. `buying_power` is
never funding. No hidden leverage.

## SELL price handling

SELL policy valuation and expected proceeds use the **canonical print** with
no premium. A SELL LIMIT is that same print (fill at limit or better is not
modeled as extra cash). Paper may credit immediately; Opaca still records
`SettlementEvent` and withholds proceeds until the derived T+1 business day.
Optimistic sell marks cannot create investable liquidity before fill and
settlement.

## Broker / database boundary

Broker I/O is **outside** `BEGIN IMMEDIATE`. Unavoidable gap: intent persisted,
process crash, submit may or may not have reached Alpaca. Recovery looks up
the deterministic id and never mints a second order. If the broker has no
order, the state stays UNKNOWN and requires operator review.

## Fresh schema-v2 demo DB

First PAPER execution uses a dedicated file such as `opaca-paper-demo.db`
(`opaca.persistence.demo.init_paper_demo_store`). It bootstraps schema v2,
verifies WAL + foreign keys, seeds the scenario once, and marks
`db_role=paper-demo`. An existing file is refused unless `overwrite=True`.
v1 files fail closed. Test DBs (`opaca.sqlite` under pytest `tmp_path`) are
a different path and role.

Reset (manual only): delete `opaca-paper-demo.db`, `-wal`, and `-shm`, then
re-init. Nothing auto-deletes demo data.

## Read-only preflight

`python -m opaca preflight` (or `pytest --live-paper-preflight`) verifies the
PAPER endpoint, ACTIVE account, cash, positions, SGOV/BIL/SHV assets, market
price, clock/calendar, demo DB, reconciliation, a 1-share SGOV BUY proposal,
TreasuryGuard, authority, and the bounded LIMIT — then **stops**. No
`submit_order`, no cancel. A passing report is not execution authority.

## Live paper mutation

Offline tests are the default. A live PAPER smoke (`BUY 1 SGOV`) runs only
with explicit `--live-paper-mutation`, present credentials, verified paper
endpoint, and a terminal warning. It is **not** chained to preflight. Before
submit it must re-run fresh quote, reconciliation, TreasuryGuard, authority,
kill switch, and bounded LIMIT validation. No stale preflight result may
authorize a later trade.
