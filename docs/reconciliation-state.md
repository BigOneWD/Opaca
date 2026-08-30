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
| Cash, positions, open orders, asset tradability | Alpaca paper account, read through `ReadOnlyAlpacaGateway` |
| Derived settlement availability | Opaca (`SettlementEvent`, T+1 calendar) over reconciled fills |
| Obligations, reserve, named policies | SQLite, seeded **once** from opening broker cash |
| Reservations, proposal identity, audit | SQLite single writer |
| Policy / authority decision | Stateless Treasury Core (`decide()`) |

SQLite never invents a second cash ledger. `BrokerCashState.cash` is the only
funding basis. `buying_power` / `regt_buying_power` /
`non_marginable_buying_power` are diagnostic metadata only.

## Broker vs local responsibilities

The application-facing gateway is a **narrow read-only capability**. Production
code receives `ReadOnlyAlpacaGateway` / `AlpacaGateway` only:

* get account, positions, asset metadata, open orders
* get order by `client_order_id` (UNKNOWN recovery)
* get calendar / clock
* reference-price read where required

The live paper adapter (`AlpacaPaperGateway`) binds those read callables at
construction and does **not** retain a mutable `TradingClient` attribute. There
is no raw client, generic request method, POST/PATCH/DELETE helper, or dynamic
broker dispatcher on the application path.

Forbidden on the phase path (and rejected by `assert_read_only_gateway`,
including nested client objects): `submit_order`, `cancel_order`,
`cancel_order_by_id`, `cancel_orders`, `replace_order`, `replace_order_by_id`,
`close_position`, `close_all_positions`, `exercise_options_position`, and
generic HTTP mutators (`post` / `put` / `patch` / `delete` / `request`).

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
with known local sell reservations or known broker unresolved SELLs, and cash
that cannot cover recorded unsettled proceeds.

## SQLite transaction semantics

* WAL mode
* `foreign_keys=ON` per connection
* `isolation_level=None` with explicit `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`
* Decimal values stored as exact decimal strings
* timestamps stored as timezone-aware ISO-8601 UTC
* schema version 1; mismatch fails closed

`evaluate_and_reserve` is one writer transaction:

1. load latest snapshot and existing proposal identity
2. reject duplicate `proposal_id` with a different payload
3. enforce current safety gates (reconciliation status, required snapshot
   version, snapshot freshness, and on replay: kill switch + approval expiry)
4. construct `PolicyContext` (including active reservations)
5. `TreasuryGuard` + authority `decide()`
6. persist decision / audit
7. if AUTO: reserve sell quantity, cash deployment, order identities
8. commit

Broker I/O happens **outside** the SQLite write lock.

`seed_scenario_once` is the same single-writer discipline: `BEGIN IMMEDIATE`,
check seed, insert scenario + obligations + audit as one unit, `COMMIT`. On
failure: `ROLLBACK`. Concurrent initializers produce exactly one seed.

## Snapshot freshness contract

Any operation capable of creating or retaining an **executable** reservation
requires a current snapshot:

* `expected_snapshot_version` is mandatory. Omitting it fails closed and does
  not reserve. It does not silently disable the version gate.
* `captured_at` must be timezone-aware UTC.
* `captured_at > now` fails closed.
* Age is `now - captured_at`. Age **greater than** `max_snapshot_age_seconds`
  is stale. The exact boundary (`age == max`) is still fresh.
* Default `max_snapshot_age_seconds` is **60** (policy row; conservative
  demo default).
* Version mismatch cannot reserve.
* Replay cannot bypass freshness, version, reconciliation status, or kill
  switch.

If state changed since the caller's snapshot: reject and retry from a fresh
reconcile. Never submit (and in this phase, never report currently executable
AUTO) from a stale evaluation.

## Replay vs current executability

Idempotent replay of a duplicate `proposal_id` must not create a second
reservation, consume authority again, or duplicate order identity.

`OrchestrationResult.is_auto` means:

> this proposal is **currently** eligible to proceed through the next
> execution gate.

It does **not** mean "this proposal was historically AUTO". Replay retains
the stored authority result and existing reservation rows, but reports
`is_auto=True` only when every current safety gate has passed.

## Reconciliation states

| Status | Meaning | May AUTO-reserve? |
| ------ | ------- | ----------------- |
| `RECONCILED` | Broker payload valid and consistent with local ledger | yes |
| `DRIFT_DETECTED` | Explicit inconsistency; snapshot persisted for audit | no |
| `UNKNOWN_REQUIRES_REVIEW` | Truth of a logical order cannot be established | no |
| `BROKER_UNAVAILABLE` | Read failed | no |
| `INVALID_BROKER_STATE` | Missing/corrupt/non-decimal/short/naive-timestamp payload, duplicate broker rows, filled > quantity, or other malformed broker input | no |

Unknown ≠ failed. Uncertainty must never create a trade.

Malformed broker input is classified as `INVALID_BROKER_STATE`. Raw
`IntegrityError` / `ValueError` / `MoneyError` / decimal or parsing exceptions
must not escape the reconciliation boundary for that input. Invalid broker
state is never persisted as `RECONCILED`.

UNKNOWN recovery looks up `client_order_id` only. It never resubmits. Not
found or lookup unavailable → `UNKNOWN_REQUIRES_REVIEW`.

### `quantity_available`

Broker position availability is authoritative state. If
`quantity_available < quantity` and neither known broker unresolved SELLs nor
known local sell reservations explain the hold-aside, reconciliation is not
clean `RECONCILED`:

* deterministically unexplained → `DRIFT_DETECTED`
* remaining sell size cannot be determined → `UNKNOWN_REQUIRES_REVIEW`

Known reservations are not double-subtracted against broker SELLs that share
the same `client_order_id`. `quantity_available > quantity` is invalid broker
state.

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

**Reservation release/lifecycle is a REQUIRED execution-phase feature.**
Until broker reconciliation can prove terminal/fill state, uncertainty retains
the reservation. This phase does **not** release reservations on imagined
fills. An AUTO SELL reservation that blocks a later opposite BUY is acceptable
fail-closed behavior for this pre-execution phase.

## Approval expiry

Persisted `APPROVAL_REQUIRED` carries `expires_at`. Expired approval is not a
valid approval:

* `now < expires_at` → still within the approval window
* `now >= expires_at` (exact boundary included) → expired

`ProposalRecord.is_currently_valid_approval(now)` and
`OrchestrationResult.approval_currently_valid(now)` expose that distinction.
Replay of an expired approval is idempotent (no extra reservation) and not
currently valid.

Human approval never overrides a hard failure. Any future execution path must
still:

```text
fresh broker reconciliation → fresh PolicyContext → TreasuryGuard re-run
```

before any submit. This phase does not execute approvals.

## Scenario seed persistence

Ratios convert to absolute amounts **once** against reconciled opening broker
cash (`treasury/scenario.py`). Later cash movements do not rescale
obligations or the operating reserve. Tests may use exact fixture values
(including `$99,999.99` from Phase −1B evidence b7).

The one-time seed is transactional (`BEGIN IMMEDIATE` … `COMMIT` / `ROLLBACK`),
including when `seed_scenario_once` is called without an explicit connection.

## Failure behavior

Every path below is fail-closed and cannot become currently executable AUTO:

* broker unavailable / malformed account, position, order, price
* duplicate broker position or order identity
* filled quantity exceeding order quantity
* missing or non-positive reference price
* SQLite busy/locked
* transaction rollback (no partial reservation)
* duplicate `proposal_id` with a different payload
* omitted, mismatched, future, or aged-out snapshot
* concurrent oversell
* ledger inconsistency (unsettled proceeds > cash)
* unsupported calendar date
* UNKNOWN lookup unresolved
* local/broker drift, including unexplained `quantity_available` hold-aside
* kill switch active (including replay of a historically AUTO proposal)
* expired approval

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
scheduler, or options/crypto path in this layer. Reservation release against
real broker fills is deferred to the execution phase.
