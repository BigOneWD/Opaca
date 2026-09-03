# Opaca demo script

## 90-second recommended cut

### 0–10 seconds — Frame the problem

“Most AI trading demos focus on finding a trade. Opaca focuses on whether the
AI should be allowed to commit the account to it.”

Show the title and the line: **AI reasons. Software enforces. Alpaca executes.**

### 10–25 seconds — Show the two lanes

Show the read-only Alpaca MCP lane flowing into AI reasoning and a bounded
`OptionIntent`. Then show the deterministic lane: contract selector, policy,
authority, atomic reservation, PAPER execution, broker readback, and
reconciliation.

Say: “The model can suggest an underlying and willingness-to-own price. It
cannot choose the OCC symbol, multiplier, quantity, limit, assignment capital,
risk thresholds, reservation, or broker mutation.”

### 25–40 seconds — Show the oversized example

Show a clearly labeled **simulated demonstration/example**, not the real XLF
trade: `$32,000 assignment obligation` versus the roughly `$25,000 hard
per-name limit`. Show deterministic `REJECT`.

Say: “The software rejects this from arithmetic. The model cannot talk its way
past the hard boundary.”

### 40–55 seconds — Show bounded repair

Show one bounded repair attempt and the compliant XLF proposal: one
`XLF260910P00058000` contract, `$5,800` assignment capital. Show
`CHECK-17..22: PASS` and `Authority: AUTO`.

### 55–75 seconds — Show the real proof

Open [the sanitized XLF evidence](../evidence/wheel-csp-proof-2026-09-03.json)
and show:

- one contract, one submit attempt
- `FILLED`
- exact short XLF PUT position
- `$5,800 ACTIVE` reservation
- `SHORT_PUT_OPEN`
- `RECONCILED`

Say: “This is a real Alpaca PAPER proof. The options quote feed was
INDICATIVE, not OPRA, and is not presented as production-grade market data.”

### 75–90 seconds — Close

“AI reasons. Software enforces. Alpaca executes. Unknown never creates the
next trade.”

## 3-minute submission cut

### 0:00–0:25 — Problem and thesis

Use the opening line above. Explain that the risk quantity is assignment
capital, not premium. A $58 PUT × 100 shares × 1 contract equals $5,800.

### 0:25–0:55 — AI and MCP boundary

Show the read-only MCP architecture. Explain that the AI returns only
`OptionIntent`: underlying, thesis/market view, willingness-to-own price, DTE
preference, and confidence. Show that authoritative contract identity,
multiplier, quantity, limit, capital, policy, authority, reservation, and
mutation are outside the model boundary.

### 0:55–1:25 — Deterministic rejection

Show the simulated oversized proposal: `$32,000` assignment obligation versus
the exact `$24,999.9850` hard per-name boundary. Label the example as
simulated. Show the failed policy result and `REJECT`. Explain the maximum two
AI attempts and the absence of recursive repair.

### 1:25–1:55 — Compliant decision

Show the XLF proposal and exact arithmetic:

```text
$58 × 100 × 1 = $5,800
$5,800 / $99,999.94 × 100 = 5.80000348...%
```

Show CHECK-17 through CHECK-22 all passing, `AUTO` authority, and the atomic
ACTIVE `$5,800` reservation.

### 1:55–2:25 — Real broker proof

Open the sanitized evidence. Show `XLF260910P00058000`, one contract, LIMIT
`$0.26`, DAY, one submit attempt, and `FILLED`. Read back the matching exact
short XLF PUT position and quantity one. Do not show credentials, raw account
IDs, or the local runtime database.

State explicitly: “This proof used Alpaca INDICATIVE options data. It is not
OPRA and is not production-grade market data.”

### 2:25–2:50 — Reconciliation and fail-closed behavior

Show `$5,800 ACTIVE`, `SHORT_PUT_OPEN`, and `RECONCILED`. Explain:
`UNKNOWN != EXPIRED`; an ambiguous order retains its reservation and cannot
create the next trade. No retry, cancel, replace, second CSP, or completed
Wheel cycle is claimed.

### 2:50–3:00 — Close

“AI reasons. Software enforces. Alpaca executes. Opaca makes capital
commitment explicit before an agent can act.”

## Presenter guardrails

- Never call the simulated `$32,000` example the real XLF trade.
- Say PAPER and INDICATIVE every time the broker proof is shown.
- Do not claim OPRA, production-grade data, profitability, assignment, expiry,
  a completed Wheel cycle, or live trading.
- Do not show `.env`, raw account identifiers, or the runtime database.
