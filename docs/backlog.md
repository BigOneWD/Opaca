# Backlog (not in this phase)

Recorded, not fixed here. Do not treat these as Phase 2 blockers.

## Bound-method `__self__` introspection escape

Read-only gateway guards inspect named attributes (`_client`, `client`,
`_trading_client`) and known mutator names. A bound method whose
`__self__` is a live `TradingClient` (or similar) is not scanned. Harden
introspection if a future adapter could smuggle a client that way.

## Reservation lifecycle / release

Implemented in Phase 3 paper execution (`opaca.execution`): resize/release
against proven broker fill/terminal state. UNKNOWN and SUBMITTING still
retain capacity. Do not release merely because a request timed out.

## Leg-order-sensitive `proposal_hash`

`proposal_hash` canonicalizes legs in list order. Two payloads with the
same legs in different order hash differently. Decide whether identity
should be order-insensitive before execution.

## Autocommit store mutators not on this execution path

Some `SQLiteStore` helpers write outside `BEGIN IMMEDIATE` when called
without an explicit connection. The evaluate/reserve path is
transactional. Remaining mutators should join that discipline if they
enter a write path.

## COMMIT-failure connection cleanup

If `COMMIT` itself fails after a successful write body, connection
recovery is not explicitly proven. Confirm rollback/close behavior
before relying on the same connection after that failure.

## Mutation scan of `spike/`

The architectural mutation scan covers `backend/opaca`. The historical
`spike/` harness is out of that gate. Keep spike evidence/tools from
being imported onto the application path.

## Non-hermetic test gate

Default pytest is offline. Live paper smoke is opt-in (`--live-paper`).
A CI/gate miss that runs live tests without intending to, or skips the
offline suite, is still an operational risk.

## Duplicate `client_order_id` detection wording

Alpaca duplicate-order detection currently string-matches broker error
wording. The safe fallback is UNKNOWN (lookup, never a second submit).
Harden if Alpaca changes the error text.

## Real broker partial fill

Paper execution resizes reservations from local/fake partial fills.
A real Alpaca partial fill has not yet been observed on the live paper
account.

## Schema migration

SQLite schema is bootstrapped at the current version. A forward-migration
path for existing files is not in this phase.

## Synthetic live-test pricing

Live paper mutation smoke uses a fixed 1-share SGOV path. Synthetic
live-test pricing for broader symbols is not in this phase.
