"""Deterministic validation for untrusted obligation-extraction JSON."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import cast

from opaca.domain.models import Obligation
from opaca.domain.money import ZERO
from opaca.intake.models import Certainty, ObligationIntakeResult, ValidatedCandidate

_TOP_LEVEL_KEYS = {"document_summary", "candidates"}
_CANDIDATE_KEYS = {
    "name",
    "amount",
    "due_date",
    "currency",
    "certainty",
    "uncertainty_reason",
    "source_excerpt",
}


class IntakeBlockedError(RuntimeError):
    """Raised when intake output is unsafe for downstream treasury use."""


def _normalized_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _require_exact_keys(data: dict[str, object], expected: set[str], label: str) -> None:
    if set(data) != expected:
        raise IntakeBlockedError(f"{label} schema keys are invalid")


def _require_non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntakeBlockedError(f"{label} must be a non-empty string")
    return value.strip()


def _parse_positive_decimal(value: object) -> Decimal:
    if not isinstance(value, str):
        raise IntakeBlockedError("amount must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise IntakeBlockedError("amount is not a valid decimal") from exc
    if not parsed.is_finite() or parsed <= ZERO:
        raise IntakeBlockedError("amount must be positive and finite")
    return parsed


def _parse_iso_date(value: object) -> date:
    if not isinstance(value, str):
        raise IntakeBlockedError("due_date must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise IntakeBlockedError("due_date must be YYYY-MM-DD") from exc


def _candidate_id(source_sha256: str, name: str, amount: Decimal, due_date: date) -> str:
    material = f"{source_sha256}|{name}|{amount}|{due_date.isoformat()}|CONFIRMED"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def parse_and_validate_extraction(
    document: str,
    raw_json: str,
    *,
    as_of: date,
) -> ObligationIntakeResult:
    """Parse one extraction response and validate the confirmed-obligation path."""
    del as_of  # Reserved for conservative treatment added by the next TDD step.
    try:
        decoded = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise IntakeBlockedError("model output is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise IntakeBlockedError("model output must be one JSON object")
    top = cast(dict[str, object], decoded)
    _require_exact_keys(top, _TOP_LEVEL_KEYS, "top-level")
    _require_non_empty_string(top["document_summary"], "document_summary")
    raw_candidates = top["candidates"]
    if not isinstance(raw_candidates, list):
        raise IntakeBlockedError("candidates must be a list")
    if len(raw_candidates) > 20:
        raise IntakeBlockedError("too many candidates")

    normalized_document = _normalized_newlines(document)
    source_sha256 = hashlib.sha256(normalized_document.encode("utf-8")).hexdigest()
    candidates: list[ValidatedCandidate] = []
    obligations: list[Obligation] = []

    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, dict):
            raise IntakeBlockedError("candidate must be a JSON object")
        candidate = cast(dict[str, object], raw_candidate)
        _require_exact_keys(candidate, _CANDIDATE_KEYS, "candidate")

        name = _require_non_empty_string(candidate["name"], "name")
        if candidate["currency"] != "USD":
            raise IntakeBlockedError("currency must be USD")
        if candidate["certainty"] != Certainty.CONFIRMED.value:
            raise IntakeBlockedError("only CONFIRMED is supported by this validation step")
        if candidate["uncertainty_reason"] is not None:
            raise IntakeBlockedError("confirmed candidate cannot have uncertainty_reason")
        source_excerpt = _require_non_empty_string(candidate["source_excerpt"], "source_excerpt")
        if _normalized_newlines(source_excerpt) not in normalized_document:
            raise IntakeBlockedError("MODEL_EVIDENCE_MISMATCH")

        amount = _parse_positive_decimal(candidate["amount"])
        due_date = _parse_iso_date(candidate["due_date"])
        obligation = Obligation(
            obligation_id=_candidate_id(source_sha256, name, amount, due_date),
            name=name,
            amount=amount,
            due_date=due_date,
        )
        validated = ValidatedCandidate(
            candidate_id=obligation.obligation_id,
            name=name,
            amount=amount,
            stated_due_date=due_date,
            effective_due_date=due_date,
            certainty=Certainty.CONFIRMED,
            uncertainty_reason=None,
            source_excerpt=source_excerpt,
            source_sha256=source_sha256,
            reserved_conservatively=False,
        )
        candidates.append(validated)
        obligations.append(obligation)

    return ObligationIntakeResult(
        source_sha256=source_sha256,
        candidates=tuple(candidates),
        effective_obligations=tuple(obligations),
        uncertain_reserved_amount=ZERO,
        trade_blocked=False,
        block_reasons=(),
    )


def require_effective_obligations(result: ObligationIntakeResult) -> tuple[Obligation, ...]:
    if result.trade_blocked:
        reasons = ", ".join(result.block_reasons)
        raise IntakeBlockedError(f"intake blocked: {reasons}")
    return result.effective_obligations
