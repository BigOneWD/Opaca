# Backlog (not in this phase)

Recorded, not fixed here. Do not treat these as Phase 2 blockers.

## Bound-method `__self__` introspection escape

Read-only gateway guards inspect named attributes (`_client`, `client`,
`_trading_client`) and known mutator names. A bound method whose
`__self__` is a live `TradingClient` (or similar) is not scanned. Harden
introspection if a future adapter could smuggle a client that way.

## Reservation lifecycle / release

Reservation release against proven broker terminal/fill state is an
execution-phase feature. This phase retains reservations under
uncertainty and does not implement release.

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
