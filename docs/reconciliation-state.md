# Reconciliation + SQLite atomic state

This phase sits on top of the closed Treasury Core. It does **not** submit,
cancel, or replace broker orders. Broker execution is **not implemented**.

```text
READ → RECONCILE → EVALUATE → RESERVE → PERSIST
```

Stop before broker order submission.

## Authoritative state boundaries

| Concern | Authority |
| ------- | --------- |
| Cash, positions, open orders, asset tradability | Alpaca paper account, read through `AlpacaGateway` |
| Derived settlement availability | Opaca (`SettlementEvent`, T+1 calendar) over reconciled fills |
| Obligations, reserve, named policies | SQLite, seeded **once** from opening broker cash |
| Reservations, proposal identity, audit | SQLite single writer |
| Policy / authority decision | Stateless Treasury Core (`decide()`) |

SQLite never invents a second cash ledger. `BrokerCashState.cash` is the only
funding basis. `buying_power` / `regt_buying_power` /
`non_marginable_buying_power` are diagnostic metadata only.

## Broker vs local responsibilities

The gateway is read-only:

* get account, positions, asset metadata, open orders
* get order by `client_order_id` (UNKNOWN recovery)
* get calendar / clock

Forbidden on the phase path: `submit_order`, `cancel_order`, `replace_order`,
`close_position`, `close_all_positions`, `exercise`.

Local SQLite owns:

* scenario seed (absolute obligations + reserve)
* snapshot versions
* proposal rows, legs, policy checks, authority decisions
* sell-quantity and cash-deployment reservations
* deterministic `client_order_id` uniqueness
* UNKNOWN / UNKNOWN_REQUIRES_REVIEW identities
* audit events

A local AUTO reservation without a broker order is **expected** in this phase
because nothing is submitted. Drift is reserved for submitted/UNKNOWN
identities missing at the broker, broker orders unknown locally, position
quantity changes versus the prior snapshot, `quantity_available` inconsistent
with local sell reservations, and cash that cannot cover recorded unsettled
proceeds.

## SQLite transaction semantics

* WAL mode
* `foreign_keys=ON` per connection
* `isolation_level=None` with explicit `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`
* Decimal values stored as exact decimal strings
* timestamps stored as timezone-aware ISO-8601 UTC
* schema version 1; mismatch fails closed

`evaluate_and_reserve` is one writer transaction:

1. load latest reconciled snapshot
2. reject stale `expected_snapshot_version`
3. construct `PolicyContext` (including active reservations)
4. `TreasuryGuard` + authority `decide()`
5. persist decision / audit
6. if AUTO: reserve sell quantity, cash deployment, order identities
7. commit

If state changed since the caller's snapshot: reject and retry from a fresh
reconcile. Never submit (and in this phase, never reserve) from a stale
evaluation.

Broker I/O happens **outside** the SQLite write lock.

## Reconciliation states

| Status | Meaning | May AUTO-reserve? |
| ------ | ------- | ----------------- |
| `RECONCILED` | Broker payload valid and consistent with local ledger | yes |
| `DRIFT_DETECTED` | Explicit inconsistency; snapshot persisted for audit | no |
| `UNKNOWN_REQUIRES_REVIEW` | Truth of a logical order cannot be established | no |
| `BROKER_UNAVAILABLE` | Read failed | no |
| `INVALID_BROKER_STATE` | Missing/corrupt/non-decimal/short/naive-timestamp payload | no |

Unknown ≠ failed. Uncertainty must never create a trade.

UNKNOWN recovery looks up `client_order_id` only. It never resubmits. Not
found or lookup unavailable → `UNKNOWN_REQUIRES_REVIEW`.

## Concurrency protection

Two threads evaluating sell-60 against a 100-share position:

* both `BEGIN IMMEDIATE`; the second waits
* the first AUTO-reserves 60 and commits
* the second loads that reservation into `PolicyContext.unresolved_orders`
* CHECK-16 rejects the oversell

SQLite uniqueness on `proposal_id` and `client_order_id` is authoritative.
Broker duplicate rejection remains future defense-in-depth only.

## Reservation lifecycle

| Authority | Persist proposal | Reserve sell/cash | Order identity | Autonomous history |
| --------- | ---------------- | ----------------- | -------------- | ------------------ |
| AUTO | yes | yes | yes | yes |
| APPROVAL_REQUIRED | yes + expiry | **no** | yes | no |
| REJECT | yes | no | yes | no |

Human approval is persisted for a later phase. Approval never overrides a
hard policy failure. A later execution path must:

```text
fresh broker reconciliation → fresh PolicyContext → TreasuryGuard re-run
```

before any submit. This phase does not execute approvals.

## Scenario seed persistence

Ratios convert to absolute amounts **once** against reconciled opening broker
cash (`treasury/scenario.py`). Later cash movements do not rescale
obligations or the operating reserve. Tests may use exact fixture values
(including `$99,999.99` from Phase −1B evidence b7).

## Failure behavior

Every path below is fail-closed and cannot become AUTO:

* broker unavailable / malformed account, position, order, price
* missing or non-positive reference price
* SQLite busy/locked
* transaction rollback (no partial reservation)
* duplicate `proposal_id` with a different payload
* stale snapshot
* concurrent oversell
* ledger inconsistency (unsettled proceeds > cash)
* unsupported calendar date
* UNKNOWN lookup unresolved
* local/broker drift

## Paper-only

Credentials come from `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`. They are
never logged or stored. The live gateway verifies the constructed client's
base URL is `https://paper-api.alpaca.markets` and refuses the live endpoint.
Default tests are offline (`FakeAlpacaGateway`). Live paper smoke runs only
with `pytest --live-paper` and present credentials, and contains no mutation
calls.

## Explicit non-implementation

**Broker execution is NOT implemented.** There is no `submit_order`,
cancel/replace, live trading, LLM proposal generation, FastAPI, UI, MCP,
scheduler, or options/crypto path in this layer.
