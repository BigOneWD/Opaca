# Wheel Task 13 Pre-Mutation Preflight

Probe date: 2026-09-03

This artifact records read-only preflight observations only. No broker order,
approval, persistent Wheel reservation, account mutation, cancellation,
replacement, exercise, close, or other broker mutation was performed. No MCP
tool was invoked; broker reads used `alpaca-py` only.

## Repository and software

- repository: `/Users/macmini/Projects/Opaca`
- branch: `feat/wheel-competition-mode`
- HEAD: `820e625f2558e3725d0293f33d6f8a8eed74364c`
- `pytest -q`: exit 0; `852 passed, 3 skipped`
- skipped tests were the explicitly gated live PAPER mutation, live PAPER
  preflight, and live PAPER read-only smoke tests
- pytest emitted one sandbox-only `PytestCacheWarning` because the repository
  cache directory is not writable; it was not a test failure
- `ruff check opaca tests`: PASS (`All checks passed!`)
- `mypy opaca tests`: PASS (`Success: no issues found in 136 source files`)
- `git diff --check`: clean
- `git status --short`: empty before this evidence artifact
- no production Python was modified

## PAPER environment and account reconciliation

The existing `/Users/macmini/Projects/Opaca/.env` was loaded without printing
secret values.

- `APCA_API_BASE_URL` exactly matched `https://paper-api.alpaca.markets`: YES
- credentials present: YES; secret values recorded: NO
- PAPER confirmed with `TradingClient(..., paper=True)` and successful PAPER
  clock/account reads: YES
- sanitized account fingerprint: `a1facbe1522d` (first 12 hex characters of
  SHA-256 of the full broker account id)
- broker clock: `2026-09-02T21:27:16.945850647-04:00`; market closed
- cash: `$99,899.58`
- equity: `$100,000.00`
- options buying power: `$99,949.79`
- options approved level: `3`
- options trading level: `3`
- account status: `ACTIVE`
- `trading_blocked`: `false`
- `transfers_blocked`: `false`
- `account_blocked`: `false`
- `trade_suspended_by_user`: `false`
- current positions: `SGOV`, long, quantity `1`, market value `$100.42`
- open orders: `0`
- unresolved option orders: `NO`; no open option order was returned

### SGOV bootstrap impact

The live account still contains one long SGOV share. Repository evidence for
the earlier Phase -1B SGOV probe says the position was sold to flat during
the B7 cleanup. Therefore the current holding cannot be safely classified
from available evidence as `KNOWN_LEGACY_NON_WHEEL_POSITION`; it is an
unexplained legacy holding for this bootstrap.

It is not adopted as a Wheel share lot and it is not sold. It reduces current
cash relative to equity by approximately `$100.42`, but the more important
bootstrap result is that the amendment requires a clean reconciliation and
fails closed on holdings that cannot be reconciled into the new Wheel state.
Fresh Wheel bootstrap is therefore BLOCKED pending separate reconciliation.

## Risk-capital proposal

No persistent production Wheel DB was seeded. Under the implemented contract,
the proposed immutable seed would be current broker cash, not the original
competition amount:

- proposed immutable `risk_capital_base`: `$99,899.58`
- hard per-name cap (`0.25 * risk_capital_base`): `$24,974.8950`
- AUTO proposal cap (`0.10 * risk_capital_base`): `$9,989.9580`
- AUTO per-name cap (`0.10 * risk_capital_base`): `$9,989.9580`
- AUTO aggregate cap (`0.20 * risk_capital_base`): `$19,979.9160`
- with multiplier `100`, dynamic hard maximum strike: `$249.74895`
- with zero existing name exposure, dynamic AUTO maximum strike: `$99.89958`

## OPRA entitlement

A direct read-only `OptionHistoricalDataClient.get_option_latest_quote` probe
was made for `IWM260904P00249000` with `feed=OptionsFeed.OPRA`.

- `OPRA_READY`: NO
- result: `APIError`; sanitized failure classification:
  `subscription/entitlement unavailable`
- no fallback from this failed OPRA probe was treated as authoritative
- indicative quotes remain available: YES
- indicative data is exploratory only and is not an authoritative mutation
  quote

## Dynamic read-only shortlist

The scan used active PUT contracts expiring 2026-09-04 through 2026-09-09,
which is the 1–7 day window under the UTC policy snapshot, with no 0-DTE
contracts. Contract metadata came from the Trading API and included an
explicit raw multiplier of `100`. Underlying quotes came from IEX; option
quotes came from the indicative feed.

| Underlying | IEX spot bid/ask | Active PUTs / hard-cap eligible | Screening contract | Assignment | Indicative bid/ask | Screening result |
|---|---:|---:|---|---:|---:|---|
| IWM | `294.02 / 294.09` | `365 / 64` | `IWM260904P00249000`, strike `249`, DTE `1`, multiplier `100` | `$24,900` | `0.00 / 0.01` | hard cap only; zero bid is not execution-usable |
| AAPL | `310.42 / unavailable` | `133 / 36` | `AAPL260904P00245000`, strike `245`, DTE `1`, multiplier `100` | `$24,500` | `0.00 / 0.01` | hard cap only; zero bid is not execution-usable |
| EEM | `67.04 / 67.19` | `74 / 74` | `EEM260909P00065000`, strike `65`, DTE `6`, multiplier `100` | `$6,500` | `0.07 / 0.14` | within hard and AUTO ceilings; strongest observed candidate |
| XLF | `57.62 / 57.67` | `133 / 133` | `XLF260908P00057000`, strike `57`, DTE `5`, multiplier `100` | `$5,700` | `0.06 / 0.13` | within hard and AUTO ceilings |

The AAPL IEX ask field was returned as zero/unavailable and is not treated as
a valid executable ask. All option observations above are indicative, not
OPRA. Representative option quote timestamps were approximately
`2026-09-02T19:11:27Z` through `2026-09-02T19:59:59Z`, materially older than
the implemented 15-second freshness limit at the broker-clock snapshot.

SPY was screened but excluded: its active PUT contracts had zero contracts
within the dynamic `$249.74895` hard strike ceiling, so it was not selected.

## Dry Wheel plan

The existing `wheel-plan` command was run with PAPER/indicative readiness
settings. It returned `SOFTWARE_READY: YES`, `MUTATION_READY: NO`, and
`DRY_RUN / SIMULATED`; its built-in fixture is SPY and is not a live candidate.
It used only a temporary test database and did not submit anything.

For the strongest live-read candidate, EEM, an equivalent in-memory dry policy
evaluation was performed without creating an approval or persistent
reservation:

- AI intent: `SELL_CASH_SECURED_PUT`; range-bound; defined ownership price;
  willing to own at or below `$65`; DTE preference `6`; confidence `0.70`
- underlying: `EEM`
- OCC: `EEM260909P00065000`
- right: `PUT`
- strike / expiry / DTE: `$65` / `2026-09-09` / `6`
- multiplier / contracts: `100` / `1`
- quote: bid `$0.07`, ask `$0.14`, feed `INDICATIVE`, timestamp
  `2026-09-02T19:59:59.599476Z`
- dry sell limit: `$0.07`
- proposed assignment capital: `$6,500`
- immutable risk-capital proposal: `$99,899.58`
- available cash: `$99,899.58`
- held-share exposure: `$100.42` SGOV, not attributed to EEM
- active reservation exposure: `$0`
- post-trade EEM exposure: `$6,500`
- post-trade aggregate Wheel exposure including current SGOV value:
  `$6,600.42`

Conditional dry check results:

- CHECK-17: PASS
- CHECK-18: PASS
- CHECK-19: FAIL — quote is older than the 15-second freshness limit
- CHECK-20: PASS
- CHECK-21: PASS
- CHECK-22: PASS
- authority: `REJECT` because a hard policy check failed

These conditional checks do not override the separate unsafe-bootstrap result
from the unexplained SGOV holding.

## Final feasibility decision

`BLOCKED_BEFORE_TASK13_MUTATION`

Exact blockers:

1. `OPRA_READY = NO`; the direct OPRA entitlement probe failed.
2. The current SGOV holding cannot be reconciled to a known Wheel state, so
   clean Wheel bootstrap fails closed.
3. The strongest observed option quote is indicative and stale, so it fails
   the authoritative 15-second quote gate.

No Task 13 mutation was started. The human-gated PAPER CSP proof remains
unready. `REAL BROKER MUTATIONS = NONE`.
