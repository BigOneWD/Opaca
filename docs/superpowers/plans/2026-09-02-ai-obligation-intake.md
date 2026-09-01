# AI Obligation Intake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a narrow, fail-closed AI obligation-intake layer that turns messy text into auditable obligations without giving the model any broker, authority, or execution power.

**Architecture:** Add a new `opaca.intake` package. Model output is treated as untrusted JSON and passes through deterministic parsing, evidence validation, certainty rules, and conservative reservation before becoming existing domain `Obligation` objects. A standard-library OpenAI-compatible provider supplies optional real-model extraction; a read-only CLI demo proves the behavior without importing or constructing broker mutation gateways.

**Tech Stack:** Python 3.11+, standard library (`json`, `urllib.request`, `hashlib`, `dataclasses`, `enum`, `decimal`, `datetime`), existing Opaca domain/treasury modules, pytest, Ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-09-02-ai-obligation-intake-design.md`

## Global Constraints

- Use strict TDD: no production code before a failing test is observed.
- Do not change TreasuryGuard checks, authority tiers, settlement rules, pricing, reconciliation, or broker execution semantics.
- Do not run `--live-paper-mutation`.
- Keep mandatory runtime dependencies empty; do not add an LLM SDK.
- The LLM may extract and explain obligations only; it may not authorize, size, or submit trades.
- Model failure must never be interpreted as an empty obligation set.
- `UNCERTAIN` with known amount reserves that amount immediately using `effective_due_date=as_of`.
- Unquantified exposure blocks downstream treasury use.
- Exact source evidence is mandatory; no fuzzy evidence matching.
- USD only for this hackathon slice.
- Maximum input: 50,000 Unicode characters. Maximum candidates: 20.

---

## File Structure

Create:

- `backend/opaca/intake/__init__.py` — public intake API exports.
- `backend/opaca/intake/models.py` — intake-specific enums/dataclasses; no provider logic.
- `backend/opaca/intake/validation.py` — strict JSON/schema parsing, evidence checks, conservative reservation, conversion to domain `Obligation`.
- `backend/opaca/intake/provider.py` — `ObligationExtractor` protocol, OpenAI-compatible stdlib HTTP provider, fixture extractor.
- `backend/opaca/intake/cli.py` — read-only `intake-demo` command implementation.
- `backend/tests/test_intake_validation.py` — fail-closed schema/evidence/conservative-reserve tests.
- `backend/tests/test_intake_provider.py` — provider contract and mocked HTTP tests.
- `backend/tests/test_intake_integration.py` — integration with `compute_liquidity` and downstream block guard.
- `backend/tests/test_intake_cli.py` — CLI output/privacy/static no-mutation tests.
- `backend/tests/fixtures/intake/messy_obligations.md` — synthetic demo document.
- `backend/tests/fixtures/intake/fixture_extraction.json` — deterministic fixture provider output.
- `backend/tests/fixtures/intake/unquantified_extraction.json` — fail-closed fixture output.

Modify:

- `backend/opaca/__main__.py` — dispatch `intake-demo` alongside `preflight`.
- `backend/pyproject.toml` only if a pytest marker is genuinely needed; do not add dependencies.

---

### Task 1: Intake types and deterministic validation

**Files:**
- Create: `backend/opaca/intake/models.py`
- Create: `backend/opaca/intake/validation.py`
- Create: `backend/opaca/intake/__init__.py`
- Test: `backend/tests/test_intake_validation.py`

**Interfaces:**
- Produces: `Certainty`, `ValidatedCandidate`, `ObligationIntakeResult`, `IntakeBlockedError`, `parse_and_validate_extraction(document: str, raw_json: str, *, as_of: date) -> ObligationIntakeResult`, `require_effective_obligations(result: ObligationIntakeResult) -> tuple[Obligation, ...]`.
- Consumes: existing `opaca.domain.models.Obligation` and money parsing behavior.

- [ ] **Step 1: Write the first failing test for a confirmed obligation**

```python
from datetime import date
from decimal import Decimal

from opaca.intake import Certainty, parse_and_validate_extraction


def test_confirmed_candidate_becomes_domain_obligation() -> None:
    document = "Payment of USD 240,000.00 is due by 12 September 2026."
    raw = """{
      "document_summary": "September payroll",
      "candidates": [{
        "name": "September payroll",
        "amount": "240000.00",
        "due_date": "2026-09-12",
        "currency": "USD",
        "certainty": "CONFIRMED",
        "uncertainty_reason": null,
        "source_excerpt": "Payment of USD 240,000.00 is due by 12 September 2026."
      }]
    }"""

    result = parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))

    assert result.trade_blocked is False
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.certainty is Certainty.CONFIRMED
    assert candidate.amount == Decimal("240000.00")
    assert candidate.stated_due_date == date(2026, 9, 12)
    assert candidate.effective_due_date == date(2026, 9, 12)
    assert candidate.reserved_conservatively is False
    assert result.effective_obligations[0].amount == Decimal("240000.00")
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
cd /Users/macmini/Projects/Opaca/backend
./.venv/bin/python -m pytest tests/test_intake_validation.py::test_confirmed_candidate_becomes_domain_obligation -q
```

Expected: FAIL because `opaca.intake` / parser does not exist.

- [ ] **Step 3: Implement minimal intake dataclasses and strict parser path**

Use exact shapes:

```python
class Certainty(StrEnum):
    CONFIRMED = "CONFIRMED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class ValidatedCandidate:
    candidate_id: str
    name: str
    amount: Decimal | None
    stated_due_date: date | None
    effective_due_date: date | None
    certainty: Certainty
    uncertainty_reason: str | None
    source_excerpt: str
    source_sha256: str
    reserved_conservatively: bool


@dataclass(frozen=True)
class ObligationIntakeResult:
    source_sha256: str
    candidates: tuple[ValidatedCandidate, ...]
    effective_obligations: tuple[Obligation, ...]
    uncertain_reserved_amount: Decimal
    trade_blocked: bool
    block_reasons: tuple[str, ...]
```

Implement `parse_and_validate_extraction()` with `json.loads`, exact top-level keys `{document_summary, candidates}`, exact candidate keys, maximum 20 candidates, exact evidence membership after only `\r\n`/`\r` -> `\n` normalization, positive decimal parsing from strings only, ISO date parsing, USD-only rule, and deterministic SHA-256 IDs.

- [ ] **Step 4: Run the first test and verify GREEN**

Run the same pytest command. Expected: PASS.

- [ ] **Step 5: Add RED tests for uncertain known amount and unquantified amount**

Add:

```python
def test_uncertain_known_amount_is_reserved_immediately() -> None:
    document = "Invoice total: USD 80,000. Terms: net 30 from receipt."
    raw = """{
      "document_summary": "Vendor invoice",
      "candidates": [{
        "name": "Vendor invoice",
        "amount": "80000.00",
        "due_date": null,
        "currency": "USD",
        "certainty": "UNCERTAIN",
        "uncertainty_reason": "Receipt date is not stated",
        "source_excerpt": "Invoice total: USD 80,000. Terms: net 30 from receipt."
      }]
    }"""
    result = parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))
    candidate = result.candidates[0]
    assert candidate.effective_due_date == date(2026, 9, 2)
    assert candidate.reserved_conservatively is True
    assert result.uncertain_reserved_amount == Decimal("80000.00")
    assert result.effective_obligations[0].due_date == date(2026, 9, 2)


def test_unquantified_obligation_blocks_downstream_use() -> None:
    document = "A regulatory payment is due this month; amount pending assessment."
    raw = """{
      "document_summary": "Regulatory payment",
      "candidates": [{
        "name": "Regulatory payment",
        "amount": null,
        "due_date": null,
        "currency": "USD",
        "certainty": "UNCERTAIN",
        "uncertainty_reason": "Amount and exact date are not stated",
        "source_excerpt": "A regulatory payment is due this month; amount pending assessment."
      }]
    }"""
    result = parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))
    assert result.trade_blocked is True
    assert "UNQUANTIFIED_OBLIGATION" in result.block_reasons
```

Run both and verify they fail for the missing behavior.

- [ ] **Step 6: Implement conservative reservation and downstream guard**

Add:

```python
class IntakeBlockedError(RuntimeError):
    pass


def require_effective_obligations(result: ObligationIntakeResult) -> tuple[Obligation, ...]:
    if result.trade_blocked:
        reasons = ", ".join(result.block_reasons)
        raise IntakeBlockedError(f"intake blocked: {reasons}")
    return result.effective_obligations
```

For `UNCERTAIN` + valid known amount + missing/uncertain due date, set `effective_due_date=as_of`, `reserved_conservatively=True`, and create a domain `Obligation` on the effective date. For `amount=None`/invalid, set `trade_blocked=True` and `UNQUANTIFIED_OBLIGATION`; never invent an amount.

- [ ] **Step 7: Add hostile-schema RED tests**

Cover at minimum:

```python
@pytest.mark.parametrize("certainty", ["Certainty.CONFIRMED", "foo.confirmed", "confirmed", ""])
def test_noncanonical_certainty_is_rejected(certainty: str) -> None: ...


def test_evidence_mismatch_blocks_entire_run() -> None: ...


def test_float_amount_is_rejected() -> None: ...


def test_extra_candidate_key_is_rejected() -> None: ...


def test_more_than_20_candidates_is_rejected() -> None: ...


def test_duplicate_normalized_candidate_is_rejected() -> None: ...
```

Verify RED, implement minimally, then verify GREEN.

- [ ] **Step 8: Run Task 1 suite and commit**

```bash
./.venv/bin/python -m pytest tests/test_intake_validation.py -q
./.venv/bin/ruff check opaca/intake tests/test_intake_validation.py
./.venv/bin/mypy --strict opaca/intake tests/test_intake_validation.py
git diff --check
git add opaca/intake tests/test_intake_validation.py
git commit -m "feat: add fail-closed obligation intake validation"
```

---

### Task 2: OpenAI-compatible extractor boundary

**Files:**
- Create: `backend/opaca/intake/provider.py`
- Test: `backend/tests/test_intake_provider.py`

**Interfaces:**
- Produces: `ObligationExtractor` protocol, `ExtractionUnavailableError`, `OpenAICompatibleObligationExtractor`, `FixtureObligationExtractor`.
- Consumes: raw document + `as_of`; returns a raw JSON string for Task 1 validator, not trusted domain objects.

- [ ] **Step 1: Write RED tests for request shape and successful extraction**

Use a fake opener injected into the provider so tests do not hit the network. Assert request URL ends with `/chat/completions`, body contains `temperature: 0`, configured model, system extraction contract, user document, and does not expose API key in returned diagnostics.

Example test shape:

```python
def test_openai_compatible_provider_returns_assistant_json() -> None:
    opener = FakeUrlOpen(status=200, body={
        "choices": [{"message": {"content": "{\"document_summary\":\"x\",\"candidates\":[]}"}}]
    })
    extractor = OpenAICompatibleObligationExtractor(
        base_url="http://127.0.0.1:8080/v1",
        model="local-model",
        api_key="super-secret",
        opener=opener,
    )
    raw = extractor.extract("No obligations.", as_of=date(2026, 9, 2))
    assert raw.startswith("{")
    assert "super-secret" not in repr(extractor)
```

Run and verify RED.

- [ ] **Step 2: Implement protocol/provider with standard library only**

Provider requirements:

```python
class ObligationExtractor(Protocol):
    provider_name: str
    def extract(self, document: str, *, as_of: date) -> str: ...
```

`OpenAICompatibleObligationExtractor` must:

- reject documents > 50,000 characters before network I/O;
- POST JSON to `<base_url.rstrip('/')>/chat/completions`;
- use a fixed 30s timeout;
- send `Authorization: Bearer ...` only when key is non-empty;
- never expose the key via `repr`, exception text, or CLI output;
- reject non-2xx, malformed transport JSON, missing `choices[0].message.content`, or non-string content;
- perform no retries.

- [ ] **Step 3: Add RED tests for timeout, non-2xx, malformed payload, oversize input, and secret redaction**

Examples:

```python
def test_timeout_becomes_intake_unavailable() -> None: ...
def test_non_2xx_becomes_intake_unavailable() -> None: ...
def test_missing_content_becomes_intake_unavailable() -> None: ...
def test_oversize_document_never_calls_network() -> None: ...
def test_exception_text_never_contains_api_key() -> None: ...
```

Implement minimal typed `ExtractionUnavailableError` handling and verify GREEN.

- [ ] **Step 4: Implement fixture extractor explicitly labeled fixture**

```python
@dataclass(frozen=True)
class FixtureObligationExtractor:
    raw_json: str
    provider_name: str = "fixture"

    def extract(self, document: str, *, as_of: date) -> str:
        return self.raw_json
```

Do not add behavior that makes fixture mode look like a real model call.

- [ ] **Step 5: Run Task 2 suite and commit**

```bash
./.venv/bin/python -m pytest tests/test_intake_provider.py -q
./.venv/bin/ruff check opaca/intake/provider.py tests/test_intake_provider.py
./.venv/bin/mypy --strict opaca/intake/provider.py tests/test_intake_provider.py
git diff --check
git add opaca/intake/provider.py tests/test_intake_provider.py
git commit -m "feat: add OpenAI-compatible obligation extractor"
```

---

### Task 3: Treasury integration and fail-closed handoff

**Files:**
- Test: `backend/tests/test_intake_integration.py`
- Modify only if needed: `backend/opaca/intake/validation.py`
- Do **not** modify: TreasuryGuard engine, reconciliation, broker execution.

**Interfaces:**
- Consumes: `require_effective_obligations()` and existing `compute_liquidity()`.
- Produces: proof that conservative AI intake changes only treasury inputs, not policy semantics.

- [ ] **Step 1: Write RED integration test for confirmed + uncertain reserve**

Use a broker cash state of `$1,000,000`, operating reserve `$400,000`, confirmed obligation `$240,000`, uncertain-known obligation `$80,000`, no settlement events.

Expected:

```python
assert projection.obligations_total == Decimal("320000.00")
assert projection.protected_liquidity == Decimal("720000.00")
assert projection.investable_cash == Decimal("280000.00")
assert projection.funding_ceiling == Decimal("280000.00")
```

This test proves the uncertain `$80k` is not accidentally investable.

- [ ] **Step 2: Run and verify RED, then make only intake-layer fixes necessary for GREEN**

Do not change `compute_liquidity()` if the existing contract already behaves correctly.

- [ ] **Step 3: Add RED test that blocked intake cannot release effective obligations**

```python
def test_unquantified_exposure_cannot_reach_treasury_math() -> None:
    result = blocked_result_for_missing_amount()
    with pytest.raises(IntakeBlockedError):
        require_effective_obligations(result)
```

Verify RED if not already covered, implement minimally, verify GREEN.

- [ ] **Step 4: Run optimized-mode safety tests**

```bash
./.venv/bin/python -O -m pytest tests/test_intake_validation.py tests/test_intake_integration.py -q
./.venv/bin/python -OO -m pytest tests/test_intake_validation.py tests/test_intake_integration.py -q
```

Expected: all targeted tests pass under both modes, proving safety does not depend on `assert`.

- [ ] **Step 5: Commit integration proof**

```bash
git add tests/test_intake_integration.py opaca/intake/validation.py
git commit -m "test: prove conservative intake treasury handoff"
```

---

### Task 4: Read-only CLI and synthetic demo fixtures

**Files:**
- Create: `backend/opaca/intake/cli.py`
- Create: `backend/tests/test_intake_cli.py`
- Create: `backend/tests/fixtures/intake/messy_obligations.md`
- Create: `backend/tests/fixtures/intake/fixture_extraction.json`
- Create: `backend/tests/fixtures/intake/unquantified_extraction.json`
- Modify: `backend/opaca/__main__.py`

**Interfaces:**
- Produces: `python -m opaca intake-demo --input ... --as-of ... --provider fixture|openai-compatible`.
- Consumes: provider extraction + Task 1 deterministic validation.

- [ ] **Step 1: Create the synthetic source fixture**

Use exactly synthetic data, for example:

```markdown
# September cash notes

Payroll team confirmed: Payment of USD 240,000.00 is due by 12 September 2026.

Vendor AP note: Invoice total: USD 80,000. Terms: net 30 from receipt. The receipt date is not available in this note.

Operations expects normal travel and software renewals this month; no additional amounts are approved in this memo.
```

The fixture JSON must cite exact excerpts from this text. One candidate is `CONFIRMED`; one is `UNCERTAIN` with `$80,000` known and date unknown.

- [ ] **Step 2: Write RED CLI test for fixture mode output**

Assert output includes:

```text
AI OBLIGATION INTAKE
PROVIDER: fixture
STATUS: CONFIRMED
STATUS: UNCERTAIN
UNCERTAIN RESERVED AMOUNT: 80000.00
HUMAN REVIEW: REQUIRED
TRADE BLOCKED: NO
BROKER MUTATION: NOT AVAILABLE IN THIS COMMAND
```

Also assert fixture mode clearly says it is not a real AI call.

- [ ] **Step 3: Implement CLI parser and dispatcher**

`backend/opaca/__main__.py` should become a two-command dispatcher:

```text
preflight    read-only PAPER checks; never submits or cancels
intake-demo  read-only AI obligation extraction; no broker mutation capability
```

`intake-demo` arguments:

- `--input PATH` required
- `--as-of YYYY-MM-DD` required
- `--provider fixture|openai-compatible` required
- optional `--fixture-json PATH` only for fixture mode; default test/demo fixture path may be explicit in the command docs, not hidden in production behavior.

For openai-compatible mode, read `OPACA_LLM_BASE_URL`, `OPACA_LLM_MODEL`, `OPACA_LLM_API_KEY`; missing base/model returns `INTAKE UNAVAILABLE`, not empty obligations.

- [ ] **Step 4: Add static RED test proving no mutation gateway access**

Parse `opaca/intake/cli.py` AST and fail if it imports/calls any forbidden broker mutation symbol/module. At minimum reject imports from:

```text
opaca.broker.mutation
opaca.execution.gateway
opaca.execution.service
```

and reject symbol names from the existing `FORBIDDEN_BROKER_MUTATIONS` set if present in the AST.

- [ ] **Step 5: Add CLI fail-closed tests**

Cover:

- unquantified fixture -> `TRADE BLOCKED: YES` + `UNQUANTIFIED_OBLIGATION`;
- evidence mismatch -> `INTAKE BLOCKED`;
- provider timeout/error -> `INTAKE UNAVAILABLE`;
- API key absent from stdout/stderr;
- help text states that a non-local configured endpoint receives the supplied document.

- [ ] **Step 6: Run CLI tests and commit**

```bash
./.venv/bin/python -m pytest tests/test_intake_cli.py -q
./.venv/bin/python -m opaca --help
./.venv/bin/python -m opaca intake-demo \
  --input tests/fixtures/intake/messy_obligations.md \
  --as-of 2026-09-02 \
  --provider fixture \
  --fixture-json tests/fixtures/intake/fixture_extraction.json

git add opaca/__main__.py opaca/intake/cli.py tests/test_intake_cli.py tests/fixtures/intake
git commit -m "feat: add read-only obligation intake demo"
```

---

### Task 5: Final verification and real-model read-only proof

**Files:**
- Modify only if verification exposes defects inside the approved intake scope.
- Do not add unrelated polish or UI.

**Interfaces:**
- Produces: release evidence for the AI-intake slice.

- [ ] **Step 1: Run full offline gates**

```bash
cd /Users/macmini/Projects/Opaca/backend
./.venv/bin/python -m pytest -q
./.venv/bin/ruff check .
./.venv/bin/ruff format --check .
./.venv/bin/mypy --strict .
git diff --check
```

Expected: all pass; report exact test count.

- [ ] **Step 2: Run all intake tests under optimized Python**

```bash
./.venv/bin/python -O -m pytest \
  tests/test_intake_validation.py \
  tests/test_intake_provider.py \
  tests/test_intake_integration.py \
  tests/test_intake_cli.py -q

./.venv/bin/python -OO -m pytest \
  tests/test_intake_validation.py \
  tests/test_intake_provider.py \
  tests/test_intake_integration.py \
  tests/test_intake_cli.py -q
```

Expected: both pass.

- [ ] **Step 3: Run one real-model read-only demo if endpoint config is available**

Do **not** load Alpaca credentials for this task. Configure only model endpoint vars.

```bash
export OPACA_LLM_BASE_URL='http://127.0.0.1:8080/v1'
export OPACA_LLM_MODEL='<configured model name>'
# OPACA_LLM_API_KEY only if required; never echo it.

./.venv/bin/python -m opaca intake-demo \
  --input tests/fixtures/intake/messy_obligations.md \
  --as-of 2026-09-02 \
  --provider openai-compatible
```

Acceptance evidence:

```text
PROVIDER: openai-compatible
one CONFIRMED obligation
one UNCERTAIN obligation
UNCERTAIN RESERVED AMOUNT: 80000.00
TRADE BLOCKED: NO
BROKER MUTATION: NOT AVAILABLE IN THIS COMMAND
```

If the chosen model fails the strict contract, stop and report the exact validation/provider reason. Do not weaken validation to make the model pass.

- [ ] **Step 4: Inspect git diff and scope**

```bash
cd /Users/macmini/Projects/Opaca
git status
git diff main...HEAD --stat
git diff main...HEAD -- backend/opaca/execution backend/opaca/reconciliation backend/opaca/policy
```

Expected: no production changes under execution/reconciliation/policy from this feature branch.

- [ ] **Step 5: Commit any final in-scope fixes separately, then return closeout report**

Return exactly:

```text
BRANCH:
HEAD:

AI INTAKE:
PASS / FAIL

REAL MODEL READ-ONLY DEMO:
PASS / NOT RUN / FAIL

CONFIRMED OBLIGATION:
...

UNCERTAIN OBLIGATION:
...

UNCERTAIN RESERVED AMOUNT:
...

UNQUANTIFIED EXPOSURE BLOCK:
PASS / FAIL

TREASURY HANDOFF:
PASS / FAIL

BROKER MUTATION CAPABILITY:
NONE

FULL TESTS:
...

OPTIMIZED -O:
...

OPTIMIZED -OO:
...

RUFF:
...

FORMAT:
...

MYPY:
...

DIFF CHECK:
...

OUT-OF-SCOPE CORE CHANGES:
NONE / list exact files

READY FOR RED-TEAM REVIEW:
YES / NO
```

Do not merge to `main` yet. The next gate is a narrow red-team review of the intake boundary, especially evidence spoofing, schema smuggling, conservative reservation, secret leakage, and static no-mutation guarantees.
