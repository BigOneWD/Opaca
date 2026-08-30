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

## T+1 settlement

Paper may credit cash immediately. Opaca records `SettlementEvent` on sell
fills and subtracts unsettled proceeds from broker cash. Proceeds become
usable on the derived T+1 business day (weekends and NYSE holidays skipped).
`buying_power` is never funding.

## Broker / database boundary

Broker I/O is **outside** `BEGIN IMMEDIATE`. Unavoidable gap: intent persisted,
process crash, submit may or may not have reached Alpaca. Recovery looks up
the deterministic id and never mints a second order. If the broker has no
order, the state stays UNKNOWN and requires operator review.

## Live paper mutation

Offline tests are the default. A live PAPER smoke (`BUY 1 SGOV`) runs only
with explicit `--live-paper-mutation`, present credentials, verified paper
endpoint, and a terminal warning. It is not run in this phase unless a human
opts in.
