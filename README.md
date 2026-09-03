# Opaca — Risk-Governed Autonomous Wheel Agent

> AI reasons. Software enforces. Alpaca executes.

Opaca is an AI-assisted options **Wheel** control plane for the problem that
usually gets skipped in trading demos: not “can the model find a trade?”, but
“should the model be allowed to commit the account to it?”

The model proposes intent. Deterministic software owns the contract identity,
assignment obligation, risk limits, authority decision, cash reservation,
paper execution boundary, broker readback, and reconciliation state.

## REAL ALPACA PAPER CSP PROOF

The primary proof is a real, reconciled Alpaca PAPER cash-secured put:

- 1 × `XLF260910P00058000` — XLF 2026-09-10 $58 PUT
- $5,800 assignment capital (`strike × multiplier × contracts`)
- CHECK-17 through CHECK-22: all PASS
- Authority: `AUTO`
- Assignment reservation: ACTIVE, $5,800
- Option submit attempts: 1
- Broker status: `FILLED`, quantity 1
- Reconciled position: exact short XLF PUT, quantity 1
- Wheel state: `SHORT_PUT_OPEN`
- Reconciliation: `RECONCILED`

Evidence: [real XLF CSP proof](docs/evidence/wheel-csp-proof-2026-09-03.json).

The competition PAPER run used Alpaca’s **INDICATIVE** options feed. It is
not OPRA and is not presented as production-grade market data. The evidence
proves the bounded PAPER execution and state transition, not profitability,
live trading, or a completed assignment/expiry/covered-call cycle.

## Why assignment capital matters

A $0.26 premium is not the main risk quantity. One $58 PUT contract controls
100 shares, so the potential assignment obligation is:

```text
$58 × 100 × 1 = $5,800
```

With the verified immutable `risk_capital_base` of $99,999.94, the proposal
was approximately 5.8% of risk capital. The configured boundaries were:

| Boundary | Exact amount |
| --- | ---: |
| Hard per-name cap (25%) | $24,999.9850 |
| AUTO proposal cap (10%) | $9,999.9940 |
| AUTO aggregate cap (20%) | $19,999.9880 |
| XLF assignment obligation | $5,800 |

Capital is fully investable. Risk is not fully delegated.

## Architecture

### Read-only intelligence lane

```text
Alpaca MCP (read-only)
        ↓
AI market reasoning
        ↓
OptionIntent
```

The verified Alpaca MCP configuration is observation-only for the competition
lane: `assets`, `stock-data`, `options-data`, and `news`, with the exposed
surface classified read-only. The exact inventory and smoke result are in
[MCP feasibility evidence](docs/evidence/wheel-readonly-feasibility-2026-09-02.md).

AI may provide:

- underlying and market view
- thesis
- willingness-to-own price
- DTE preference
- confidence

AI may not control:

- OCC symbol or multiplier
- contract count or sell limit
- assignment capital
- risk thresholds or authority
- reservation state
- broker mutation

### Deterministic authority lane

```text
OptionIntent
    ↓
Contract selector
    ↓
Hard risk policy: CHECK-17..22
    ↓
Delegated authority
    ↓
Atomic assignment-capital reservation
    ↓
alpaca-py PAPER execution
    ↓
Broker readback by deterministic client ID
    ↓
Wheel reconciliation
```

The AI boundary allows at most two intent attempts. A hard policy rejection
can return bounded repair facts, but the model cannot override a policy check,
expand the contract, or recurse indefinitely.

## Fail-closed behavior

`UNKNOWN != EXPIRED`. If a broker submission or readback is ambiguous, Opaca
keeps the full assignment reservation active. An uncertain order cannot free
capital and cannot create the next Wheel trade. Only exact broker facts and
the matching local reservation can produce `SHORT_PUT_OPEN`, `SHARES_HELD`, or
a proven terminal release.

## Run the offline verification suite

```bash
cd backend
./.venv/bin/pytest tests/wheel -q
./.venv/bin/ruff check opaca tests --no-cache
./.venv/bin/mypy --cache-dir /private/tmp/opaca-mypy-final opaca tests
```

The latest full pre-runtime verification recorded `852 passed, 3 skipped`.
After the runtime PAPER bootstrap, the Wheel-specific and critical safety
suites pass. One unrelated intake-provider test cannot execute in the
restricted Codex sandbox because localhost `socket.bind` is denied:
`tests/test_intake_provider.py::test_provider_does_not_follow_redirect_to_another_origin_with_bearer`.

## Limitations and honest scope

- PAPER trading only; no live-trading claim.
- The real CSP proof used INDICATIVE data, not OPRA.
- Current Wheel state is `SHORT_PUT_OPEN` only.
- No profitability, P&L, assignment, expiry, or covered-call result is claimed.
- Covered-call progression is future work.
- Production deployment would require higher-quality market data, operational
  controls, and further validation.

An earlier [BTC PAPER round-trip proof](docs/evidence/btc-paper-roundtrip-proof-2026-09-03.md)
demonstrates independent 24/7 execution/readback plumbing; the XLF CSP is the
primary Wheel proof.

## Submission materials

- [Project description](docs/submission/project-description.md)
- [One-page writeup](docs/submission/one-page-writeup.md)
- [Demo script](docs/submission/demo-script.md)
- [Submission checklist](docs/submission/submission-checklist.md)

No public repository push or Devpost write was performed by this repository
pass. External submission assets and form fields remain manual actions.
