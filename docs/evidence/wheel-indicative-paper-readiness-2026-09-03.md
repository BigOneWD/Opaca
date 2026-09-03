# Competition PAPER Indicative Readiness

Probe date: 2026-09-03

This artifact records the explicitly approved competition-PAPER feed decision
and the read-only precheck for the one-share SGOV cleanup. It does not claim
that INDICATIVE equals OPRA or that the workflow is production-ready.

## Deliberate feed decision

For this Alpaca competition PAPER workflow only, a fresh valid INDICATIVE
option quote may satisfy `PAPER_MUTATION_READY` after every existing
deterministic execution gate passes. `PRODUCTION_GRADE_MARKET_DATA` remains
`NO` for INDICATIVE. OPRA remains the preferred authoritative feed.

The concrete execution gateway remains PAPER-only. No option order was
submitted and no live/production execution mode was added.

## Software verification

- branch: `feat/wheel-competition-mode`
- RED test commit: `8fe1705` (`test: define indicative competition paper pricing`)
- focused tests: `31 passed`
- GREEN commit: `d12084d` (`feat: allow fresh indicative quotes for competition paper`)
- full `pytest -q`: `852 passed, 3 skipped`; exit 0
- Ruff: PASS
- mypy: PASS (`137 source files`)
- `git diff --check`: clean before this evidence artifact
- quote age, future timestamp, bid/ask, premium, risk, reservation, account
  binding, final recheck, UNKNOWN, and deterministic identity gates remain
  covered by existing behavior/tests

The readiness evidence now distinguishes:

- `PAPER_MUTATION_READY`: competition-PAPER execution readiness
- `PRODUCTION_GRADE_MARKET_DATA`: `YES` only for OPRA-quality data
- INDICATIVE notice: accepted for competition PAPER execution; not presented
  as OPRA or production-grade market data

## SGOV cleanup read-only precheck

Credentials were loaded from `/Users/macmini/Projects/Opaca/.env` without
printing secret values.

- `APCA_API_BASE_URL` exactly matched `https://paper-api.alpaca.markets`: YES
- PAPER client/read path: verified
- broker clock: `2026-09-02T22:00:49.314327-04:00`
- regular market open: `NO`
- next regular open: `2026-09-03T09:30:00-04:00`
- SGOV position before cleanup: `1` long share, market value `$100.42`
- SGOV cleanup order before submission: none open
- cleanup submission: `NO`
- submit attempts: `0`
- cleanup status: `SGOV_CLEANUP = WAITING_FOR_REGULAR_MARKET_OPEN`
- SGOV position after: unchanged, `1` long share
- other broker mutations: `NONE`

Because the regular session was closed, no overnight or extended-hours order
was queued. Phase B stopped before mutation as required.

## Deferred phases

- clean account reconciliation: `BLOCKED / NOT REACHED`; pending SGOV removal
- persistent Wheel DB bootstrap: `NO / NOT REACHED`
- risk capital seed: not persisted; the prior read-only proposal from current
  cash was `$99,899.58`
- proposed hard per-name cap: `$24,974.8950`
- proposed AUTO per-name/proposal cap: `$9,989.9580`
- proposed AUTO aggregate cap: `$19,979.9160`
- fresh option shortlist and dry plan: `NOT REACHED`; the market-hours gate
  stopped the workflow before fresh INDICATIVE option reads
- `PAPER_MUTATION_READY`: `NO` for this run; it was not established because
  the regular-session cleanup gate blocked before fresh quote validation
- `PRODUCTION_GRADE_MARKET_DATA`: `NO` for INDICATIVE

## Final status

Exact next step: rerun the read-only SGOV precheck during regular US equity
market hours. Submit at most the explicitly authorized single `SELL 1 SGOV`
PAPER order only if `is_open=true` and every other cleanup precondition still
passes. Do not start the CSP before human review.

`REAL OPTION ORDERS SUBMITTED = NONE`
