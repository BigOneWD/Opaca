# Opaca — One-page writeup

## Problem

An AI model can suggest a plausible options trade in seconds, but a plausible
trade can still create an unacceptable account obligation. In a Wheel strategy,
the premium is easy to notice and the assignment risk is easy to understate. A
$58 put with a 100-share multiplier is not a $26 decision: it is a potential
$5,800 commitment. Opaca is built around that distinction.

## AI role

Opaca gives the AI a narrow reasoning role. It may propose an `OptionIntent`
containing the underlying, market view, thesis, willingness-to-own price, DTE
preference, and confidence. The provider has no broker client and cannot submit
orders. It cannot choose the authoritative OCC symbol, multiplier, quantity,
limit, assignment capital, policy thresholds, authority, or reservation.

The read-only intelligence lane can use the configured Alpaca MCP surface for
assets, stock data, options data, news, and reference documentation. The MCP
surface is observation-only. The AI's output is treated as untrusted input and
is bounded to at most two intent attempts. A hard rejection can provide narrow
repair facts, but the AI cannot override a failed policy check or enter a
recursive prompt loop.

## Deterministic controls

The authority lane selects an authoritative active/tradable contract, validates
fresh quote data, calculates assignment capital using exact decimals, and runs
CHECK-17 through CHECK-22. Those checks cover runtime safety, contract identity,
quote economics and freshness, exposure/cash, broker collateral diagnostics,
and account binding. Delegated authority is evaluated separately from the
model's reasoning.

Before broker I/O, SQLite `BEGIN IMMEDIATE` creates one exact ACTIVE
assignment-capital reservation and persists the deterministic logical order
identity. The immutable risk capital base for the proof account was $99,999.94.
The hard per-name boundary was $24,999.9850; the AUTO proposal boundary was
$9,999.9940; and the AUTO aggregate boundary was $19,999.9880. The selected
$5,800 obligation was approximately 5.8% of risk capital and passed the AUTO
envelope.

## Real PAPER proof

The real run used `XLF260910P00058000`: one XLF 2026-09-10 $58 PUT, multiplier
100, with a fixed minimum LIMIT of $0.26. Two fresh INDICATIVE quotes passed
the local gates. CHECK-17 through CHECK-22 all passed, authority was AUTO, and
the $5,800 reservation was committed before submission.

The existing `alpaca-py` PAPER gateway made exactly one SELL TO OPEN submission:
one contract, LIMIT $0.26, DAY. Broker readback returned the exact client order
ID and OCC symbol, SELL side, quantity one, and FILLED status. The broker then
showed the exact short XLF PUT position with quantity one. The reservation
remained ACTIVE, and reconciliation persisted `SHORT_PUT_OPEN` with
`RECONCILED` status. The sanitized record is linked at
[`docs/evidence/wheel-csp-proof-2026-09-03.json`](../evidence/wheel-csp-proof-2026-09-03.json).

## Why it matters

Opaca makes the account commitment explicit and machine-enforced. Capital is
fully investable; risk is not fully delegated. This is a practical pattern for
AI-native financial access: let the model help with context and hypotheses,
then keep the irreversible or capital-binding decisions behind typed policy,
authority, reservation, and reconciliation boundaries.

## Reconciliation and limitations

`UNKNOWN != EXPIRED`. If a broker call times out or readback is contradictory,
Opaca keeps the reservation active and blocks the next Wheel trade until exact
facts arrive. The present proof is PAPER-only and used the Alpaca INDICATIVE
feed, not OPRA. INDICATIVE is not production-grade market data. This artifact
does not claim profitability, live trading, assignment, expiry, a completed
Wheel cycle, or a covered-call leg. Production deployment would require higher-
quality market data, operational controls, and further validation.

The current state is intentionally narrow: `SHORT_PUT_OPEN`. The next Wheel
transition is future work, not implied by this proof.
