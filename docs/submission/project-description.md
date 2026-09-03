# Opaca — Risk-Governed Autonomous Wheel Agent

## Paste-ready short description

AI reasons. Software enforces. Alpaca executes. Opaca turns an AI-proposed options Wheel intent into a bounded, paper-only, reconciled action without letting the model control assignment risk.

## Paste-ready long description

Most AI trading demos ask whether a model can find a trade. Opaca asks the
harder question: should the model be allowed to commit the account to it?

Opaca is a risk-governed autonomous Wheel agent for options. The AI contributes
bounded intent: an underlying, market view, thesis, willingness-to-own price,
DTE preference, and confidence. That intent is untrusted input. Deterministic
software then selects the authoritative OCC contract, reads the broker quote,
calculates assignment capital, applies hard policy checks, decides whether the
proposal fits delegated authority, reserves the cash obligation atomically,
and only then reaches the narrow Alpaca PAPER execution boundary.

The central risk model is assignment capital, not premium. A single $58 PUT
contract with a 100-share multiplier represents a potential $5,800 assignment
obligation. Opaca stores that amount as an active reservation before submission.
With an immutable risk capital base of $99,999.94, the proposal was about 5.8%
of risk capital. The hard per-name limit was $24,999.9850, the AUTO proposal
limit was $9,999.9940, and the AUTO aggregate limit was $19,999.9880. The
$5,800 proposal passed all six Wheel checks and remained inside the delegated
AUTO envelope.

The architecture has two intentionally separate lanes. In the read-only
intelligence lane, Alpaca MCP supplies market context through the configured
assets, stock-data, options-data, and news toolsets. The AI can reason over
that context, but it receives no authority over OCC identity, multiplier,
quantity, limit price, assignment capital, policy thresholds, reservations, or
broker mutation. In the deterministic authority lane, the validated intent
passes through contract selection, CHECK-17 through CHECK-22, AUTO authority,
SQLite `BEGIN IMMEDIATE` reservation, a paper-only `alpaca-py` gateway, broker
readback by deterministic client order ID, and Wheel reconciliation.

The primary real proof is one XLF cash-secured put: `XLF260910P00058000`, one
contract, XLF 2026-09-10 $58 PUT, with a persisted minimum sell limit of $0.26.
The order was submitted once to Alpaca PAPER, filled for one contract, and
read back with the exact client ID, OCC symbol, SELL side, quantity, LIMIT
$0.26, and DAY time-in-force. The broker position was a matching short XLF
PUT with quantity one. The $5,800 assignment reservation stayed ACTIVE, and
Opaca reconciled the state to `SHORT_PUT_OPEN` with `RECONCILED` status.

The competition run used Alpaca’s INDICATIVE options feed. This is explicitly
not OPRA and is not presented as production-grade market data. The proof is a
bounded PAPER execution and reconciliation demonstration, not a profitability
claim. It does not claim an assignment, expiry, covered-call leg, completed
Wheel cycle, or live trading.

Failure behavior is part of the product. `UNKNOWN != EXPIRED`: an ambiguous
broker outcome retains the full reservation and cannot create the next Wheel
trade. The AI gets at most two intent attempts, with narrow repair feedback;
hard policy checks cannot be overridden by the model. This makes the system
useful as an agentic financial-access control plane: AI can help reason about
choices, while software prevents an otherwise plausible conversation from
becoming an unbounded account commitment.

The repository contains the typed Python domain, exact-decimal policy and
reservation logic, read-only MCP surface guard, Alpaca adapters, paper
execution boundary, reconciliation state machine, tests, and sanitized proof
artifacts. The implementation is deliberately honest about what remains:
PAPER-only operation, indicative options data, further production validation,
and future covered-call progression.

## Form-specific verification before posting

The live Build with Gemini XPRIZE requirements also ask for evidence of the
Google Cloud product and Gemini API usage, revenue and expense evidence,
customer evidence, a three-minute video, and the required repository/file
deliverables. Those items are tracked as manual blockers in
[the submission checklist](submission-checklist.md); this repository does not
claim evidence that is not present.
