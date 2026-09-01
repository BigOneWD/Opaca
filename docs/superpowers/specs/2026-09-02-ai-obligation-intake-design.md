# AI Obligation Intake Design

**Date:** 2026-09-02

**Status:** Approved design, pre-implementation

## 1. Goal

Add one narrow AI reasoning layer to Opaca that converts messy corporate cash-obligation text into structured, auditable obligation candidates, then hands only deterministically validated obligations to the existing treasury core.

The feature exists to make the boundary explicit:

> AI reasons. Software enforces. Alpaca executes.

The AI may extract and explain obligations. It may not authorize trades, calculate broker truth, mutate execution state, or bypass TreasuryGuard.

## 2. Scope

### In scope

- Read unstructured UTF-8 text or Markdown such as invoices, payroll notes, tax notices, and vendor payment notices.
- Ask an OpenAI-compatible chat-completions endpoint to extract obligation candidates into a strict JSON contract.
- Validate the model output deterministically before any candidate can affect treasury math.
- Preserve exact source evidence for every extracted candidate.
- Distinguish `CONFIRMED` from `UNCERTAIN` obligations.
- Conservatively reserve a known amount when its due date is uncertain.
- Block downstream treasury use when the document reveals a possible liability whose amount cannot be quantified safely.
- Feed effective validated obligations into the existing `Obligation` / `compute_liquidity` path without changing TreasuryGuard semantics.
- Provide a read-only `python -m opaca intake-demo ...` command that never opens a broker mutation gateway.
- Include deterministic offline fixtures and tests plus an optional real-model demo path.

### Out of scope

- PDF/OCR extraction.
- Email, ERP, bank, Drive, or accounting-system integrations.
- LLM-generated BUY/SELL decisions.
- LLM-generated order sizing.
- Changes to TreasuryGuard checks, authority tiers, settlement rules, pricing, reconciliation, or broker execution.
- Vector databases, RAG, agent frameworks, tool calling, fine-tuning, custom ML, or multi-agent orchestration.
- UI/dashboard work.
- The separate $1M treasury-value model and T+0/T+1/T+2 visualization; those are follow-on slices.

## 3. Existing constraints we preserve

Opaca already models `Obligation` as a typed dated liability with positive `amount` and explicit `due_date`. The settlement-aware liquidity engine subtracts obligations and operating reserve from reconciled settled cash to derive investable cash. This feature must adapt into that contract rather than weakening it.

The current core has no mandatory runtime dependencies. Keep it that way. The OpenAI-compatible provider will use Python standard-library HTTP and JSON facilities rather than adding a model SDK.

The existing paper-trading and execution core is frozen for this slice. No live-paper mutation test is part of this work.

## 4. Architecture

```text
UTF-8 document
    |
    v
ObligationExtractor protocol
    |
    +-- OpenAICompatibleObligationExtractor  (real optional model path)
    +-- FixtureObligationExtractor           (offline tests only, explicitly labeled)
    |
    v
ExtractionEnvelope (untrusted model-shaped data)
    |
    v
Deterministic intake validator
    |
    +-- exact evidence check against source text
    +-- strict amount/date parsing
    +-- candidate-count / field-size bounds
    +-- certainty rules
    +-- stable candidate identity
    |
    v
ObligationIntakeResult
    |
    +-- candidates: CONFIRMED / UNCERTAIN
    +-- effective_obligations: tuple[Obligation, ...]
    +-- uncertain_reserved_amount
    +-- trade_blocked + block_reasons
    |
    v
require_effective_obligations(result)
    |
    +-- blocked result -> raises IntakeBlockedError
    +-- safe result -> tuple[Obligation, ...]
    |
    v
existing compute_liquidity / TreasuryGuard inputs
```

The extractor boundary is intentionally untrusted. Model output is data, not authority.

## 5. Data contract

### 5.1 Model extraction schema

The model must return exactly one JSON object, with no Markdown fence and no prose around it:

```json
{
  "document_summary": "short factual summary",
  "candidates": [
    {
      "name": "Quarterly GST payment",
      "amount": "80000.00",
      "due_date": "2026-09-15",
      "currency": "USD",
      "certainty": "CONFIRMED",
      "uncertainty_reason": null,
      "source_excerpt": "Payment of USD 80,000 is due by 15 September 2026."
    }
  ]
}
```

Accepted `certainty` values are only:

- `CONFIRMED`
- `UNCERTAIN`

`amount` and `due_date` may be `null` only for an `UNCERTAIN` candidate. The top-level object and candidate objects are strict: unknown extra keys are rejected rather than ignored.

The model is explicitly instructed not to infer a missing date from common payment conventions and not to convert relative language unless the source provides the anchor needed to do so.

### 5.2 Validated candidate

Add a separate intake-layer type rather than extending the core `Obligation` model with AI metadata.

A validated candidate contains:

- stable `candidate_id`
- `name`
- `amount: Decimal | None`
- `stated_due_date: date | None`
- `effective_due_date: date | None`
- `certainty: CONFIRMED | UNCERTAIN`
- `uncertainty_reason: str | None`
- exact `source_excerpt`
- `source_sha256`
- `reserved_conservatively: bool`

The stable identity is derived from the source hash plus normalized candidate fields. It is evidence identity, not a broker identity.

## 6. Deterministic validation and fail-closed rules

### 6.1 Source evidence is mandatory

Every candidate must contain a non-empty `source_excerpt` that appears exactly in the input document after newline normalization only. No fuzzy match, semantic match, substring reconstruction, or model-generated paraphrase is accepted as evidence.

If a model emits a candidate whose evidence is not present in the document, the intake result is blocked with `MODEL_EVIDENCE_MISMATCH`. No effective obligations are released downstream from that extraction run.

### 6.2 Confirmed obligation

A candidate becomes `CONFIRMED` only when all of the following are true:

- model certainty is exactly `CONFIRMED`;
- amount parses as a positive exact decimal;
- due date parses as an ISO `YYYY-MM-DD` date;
- currency is exactly `USD` for this hackathon slice;
- source evidence matches exactly;
- no uncertainty reason is present.

A confirmed candidate converts directly to the existing domain `Obligation` using its stated due date.

### 6.3 Uncertain due date with known amount

If the amount is known and valid but the due date is missing or explicitly uncertain, the candidate remains `UNCERTAIN` and is conservatively reserved.

For treasury math only, its `effective_due_date` is the intake `as_of` date. This does **not** rewrite the source claim; `stated_due_date` remains `None` or the uncertain date supplied by the model, while `effective_due_date=as_of` is clearly labeled as a conservative policy treatment.

The resulting effective `Obligation` therefore removes that amount from current investable surplus immediately until a human resolves the date.

### 6.4 Unquantified exposure

If a plausible obligation is extracted but its amount is missing or invalid, Opaca cannot know how much cash to reserve. The whole intake result sets:

- `trade_blocked = True`
- reason `UNQUANTIFIED_OBLIGATION`

`require_effective_obligations(result)` raises `IntakeBlockedError` for this result. Callers therefore cannot accidentally treat a blocked intake as an empty obligation set. This slice does not modify the existing orchestration module; the safe accessor is the integration boundary used by new intake-aware callers and tests.

### 6.5 Invalid or hostile model output

The following block the intake run rather than being silently ignored:

- invalid top-level JSON;
- extra prose around JSON;
- unknown extra schema keys;
- wrong schema types;
- more than 20 candidates;
- non-USD currency in this slice;
- binary floats for monetary values;
- negative/zero amounts;
- malformed dates;
- evidence mismatch;
- duplicate normalized candidates;
- empty required strings;
- arbitrary/dotted certainty values.

Unknown model output does not become an obligation and does not create a trade.

## 7. Provider boundary

Define a narrow protocol similar to:

```python
class ObligationExtractor(Protocol):
    def extract(self, document: str, *, as_of: date) -> ExtractionEnvelope: ...
```

### OpenAI-compatible provider

Environment variables:

- `OPACA_LLM_BASE_URL`
- `OPACA_LLM_MODEL`
- `OPACA_LLM_API_KEY` (optional for local endpoints)

Provider behavior:

- use `/chat/completions`;
- normalize one trailing slash on the configured base URL before appending the endpoint path;
- `temperature=0`;
- a fixed system prompt containing the extraction schema and no-guessing rules;
- fixed request timeout of 30 seconds;
- maximum input size of 50,000 Unicode characters;
- never log or print the API key;
- never print request headers;
- reject non-2xx responses, malformed JSON, missing assistant content, and content that is not exactly one JSON object;
- no retries that could create hidden nondeterminism in the demo command; a failed extraction is surfaced as blocked/unavailable.

The provider may use `urllib.request` and `json` from the standard library so core dependencies remain unchanged.

### Fixture provider

A fixture extractor is permitted only for unit/integration tests and explicitly offline demo fixtures. Its output must be labeled `provider=fixture`; it must never be presented as a real AI call in hackathon evidence.

## 8. CLI demo

Extend the CLI with a read-only command:

```bash
python -m opaca intake-demo \
  --input tests/fixtures/intake/messy_obligations.md \
  --as-of 2026-09-02 \
  --provider openai-compatible
```

Optional offline test/demo mode:

```bash
python -m opaca intake-demo \
  --input tests/fixtures/intake/messy_obligations.md \
  --as-of 2026-09-02 \
  --provider fixture
```

Output is text-first and greppable, for example:

```text
AI OBLIGATION INTAKE
PROVIDER: openai-compatible
SOURCE SHA256: ...

CANDIDATE 1
STATUS: CONFIRMED
NAME: September payroll
AMOUNT: 240000.00
STATED DUE DATE: 2026-09-12
EFFECTIVE DUE DATE: 2026-09-12
EVIDENCE: "..."

CANDIDATE 2
STATUS: UNCERTAIN
NAME: Vendor invoice
AMOUNT: 80000.00
STATED DUE DATE: n/a
EFFECTIVE DUE DATE: 2026-09-02
RESERVED CONSERVATIVELY: YES
HUMAN REVIEW: REQUIRED
EVIDENCE: "..."

UNCERTAIN RESERVED AMOUNT: 80000.00
TRADE BLOCKED: NO
BROKER MUTATION: NOT AVAILABLE IN THIS COMMAND
```

If any unquantified exposure or invalid model evidence occurs:

```text
TRADE BLOCKED: YES
BLOCK REASON: UNQUANTIFIED_OBLIGATION
BROKER MUTATION: NOT AVAILABLE IN THIS COMMAND
```

The command must not import or construct the paper mutation gateway.

## 9. Treasury-core integration

The intake layer exposes `effective_obligations`, but intake-aware callers must obtain them through `require_effective_obligations(result)`. That accessor raises on any blocked result and returns the typed obligation tuple only for a safe result.

An integration test will prove:

1. a confirmed $240k obligation becomes an ordinary `Obligation`;
2. an uncertain $80k obligation with no reliable due date is converted to an effective obligation due `as_of`;
3. both amounts are subtracted by the existing `compute_liquidity` implementation;
4. the uncertain $80k therefore cannot appear in investable surplus;
5. an unquantified candidate makes `require_effective_obligations()` fail closed, so no intake-aware caller can obtain an executable empty-obligation view from that result.

No TreasuryGuard or orchestration check is altered. The new layer only makes inputs more realistic and conservative.

## 10. Demo fixtures

Create one messy document fixture with at least:

- a clear payroll obligation with explicit USD amount and date;
- a vendor invoice with explicit amount but ambiguous date, e.g. `net 30 from receipt` where the receipt date is not stated;
- unrelated narrative text so extraction is not a trivial line parser.

Create a second hostile fixture or model-output fixture containing an obligation with a missing amount to demonstrate `UNQUANTIFIED_OBLIGATION` fail-closed behavior.

Fixtures must not contain real company or personal data.

## 11. Error handling

All intake-specific failures use typed exceptions/results and become one of two user-visible outcomes:

- `INTAKE BLOCKED` — model output or source evidence is unsafe/invalid, or an exposure cannot be quantified;
- `INTAKE UNAVAILABLE` — provider/network/model endpoint could not return a valid response.

Neither outcome is converted into `no obligations`. Treating model failure as an empty obligation set would inflate investable surplus and is forbidden.

## 12. Security and privacy

- Do not print API secrets.
- Do not persist source documents or model responses by default.
- CLI may print short exact evidence excerpts already present in the user-supplied source.
- Cap source size and candidate count to bound accidental prompt/data expansion.
- The provider sends the supplied document to the configured endpoint; the CLI help text must state this plainly for non-local endpoints.

## 13. Testing strategy

Use TDD and keep all broker mutation paths out of scope.

Required coverage:

- exact JSON schema parsing, including rejection of extra keys;
- real `Decimal` parsing, no binary float acceptance;
- ISO date parsing;
- exact evidence-match success/failure;
- confirmed candidate conversion;
- uncertain-known-amount conservative reservation;
- unquantified exposure blocks downstream use;
- `require_effective_obligations()` cannot return obligations for blocked intake;
- malformed/hostile model responses fail closed;
- candidate-count and document-size limits;
- secret redaction/no key in diagnostics;
- provider HTTP success/error/timeout using mocked stdlib HTTP;
- CLI fixture mode;
- CLI openai-compatible mode with mocked provider;
- CLI source/privacy messaging;
- integration with existing `compute_liquidity` showing uncertain funds excluded from investable cash;
- static test proving `intake-demo` cannot call broker mutation functions;
- `python -O` and `python -OO` targeted suites to ensure safety does not rely on asserts.

Full repository gates remain:

```text
pytest
ruff check .
ruff format --check .
mypy --strict
git diff --check
```

No `--live-paper-mutation` run is permitted for this feature.

## 14. Acceptance criteria

The slice is complete when:

1. A real OpenAI-compatible model can parse the messy demo document into the strict extraction contract.
2. At least one clear obligation becomes `CONFIRMED` with exact source evidence.
3. At least one ambiguous obligation becomes `UNCERTAIN`, is reserved conservatively, and is visibly flagged for human review.
4. An unquantified possible liability blocks downstream use rather than disappearing from treasury math.
5. The existing liquidity engine demonstrably excludes all effective confirmed/conservatively-reserved obligations from investable surplus.
6. The command has no broker mutation capability.
7. Offline, optimized-mode, lint, format, typing, and diff gates pass.
8. Demo copy never implies fixture mode is a real AI call.

## 15. Follow-on slices, deliberately not included here

After this slice is merged, implement separately:

1. `$1M MODEL` treasury-value scenario with a named non-zero cash baseline, live-dated market inputs, expense ratio/spread cost, and recomputable incremental income.
2. T+0/T+1/T+2 time-to-cash presentation using the settlement logic already present in the treasury core.
3. LIVE vs MODEL evidence panels and adversarial-scenario denominator for the final hackathon demo.

Keeping these separate prevents submission work from destabilizing the already-completed real-paper execution core.
