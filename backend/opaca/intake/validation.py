"""Deterministic validation for untrusted obligation-extraction JSON."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import cast

from opaca.domain.models import Obligation
from opaca.domain.money import ZERO, MoneyError, positive_money
from opaca.intake.models import (
    MAX_CANDIDATE_NAME_CHARS,
    MAX_DOCUMENT_CHARS,
    MAX_DOCUMENT_SUMMARY_CHARS,
    MAX_MODEL_RESPONSE_CHARS,
    MAX_SOURCE_EXCERPT_CHARS,
    MAX_UNCERTAINTY_REASON_CHARS,
    Certainty,
    IntakeBlockedError,
    ObligationIntakeResult,
    ValidatedCandidate,
)

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
_MONEY_ANCHOR_RE = re.compile(r"\bUSD[ \t]+(?P<amount>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\b")
_CANONICAL_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_ISO_DATE_ANCHOR_RE = re.compile(r"(?<!\d)(?P<value>\d{4}-\d{2}-\d{2})(?!\d)")
_LONG_DATE_ANCHOR_RE = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})[ \t]+"
    r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)"
    r"[ \t]+(?P<year>\d{4})(?!\d)"
)
_NEGATED_DUE_DATE_PREFIX_RE = re.compile(
    r"(?:\b(?:is|was|will)[ \t]+not[ \t]+"
    r"(?:yet[ \t]+|currently[ \t]+)?(?:be[ \t]+)?(?:due|payable)"
    r"|\b(?:isn|wasn|won)['’]t[ \t]+"
    r"(?:yet[ \t]+|currently[ \t]+)?(?:be[ \t]+)?(?:due|payable)"
    r"|\bno[ \t]+longer[ \t]+(?:due|payable)"
    r"|\bnever[ \t]+(?:due|payable)"
    r"|\bnot[ \t]+(?:due|payable))"
    r"(?:[ \t]+date)?[ \t]*(?:is[ \t]*)?"
    r"(?:on|by)?[ \t]*:?[ \t]*$",
    re.IGNORECASE,
)
_DUE_DATE_PREFIX_RE = re.compile(
    r"(?:\b(?:due|payable)(?:[ \t]+date)?[ \t]*(?:is[ \t]*)?"
    r"(?:on|by)?[ \t]*:?[ \t]*"
    r"|\bpayment[ \t]+(?:is[ \t]+)?due[ \t]*(?:on|by)?[ \t]*:?[ \t]*"
    r"|\bpayment[ \t]+date[ \t]*:?[ \t]*)$",
    re.IGNORECASE,
)
_DATE_LIST_CONNECTOR_RE = re.compile(r"(?:,|\b(?:or|and)\b)[ \t]*$", re.IGNORECASE)
_MONTHS = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}


class _DuplicateJSONKeyError(ValueError):
    """Raised when a JSON object repeats a key instead of overwriting it."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError
        result[key] = value
    return result


def _normalized_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _require_exact_keys(data: dict[str, object], expected: set[str], label: str) -> None:
    if set(data) != expected:
        raise IntakeBlockedError(f"{label} schema keys are invalid")


def _require_non_empty_string(value: object, label: str, max_chars: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntakeBlockedError(f"{label} must be a non-empty string")
    result = value.strip()
    if max_chars is not None and len(result) > max_chars:
        raise IntakeBlockedError(f"{label} exceeds length limit")
    return result


def _parse_positive_decimal(value: object) -> Decimal:
    if not isinstance(value, str):
        raise IntakeBlockedError("amount must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise IntakeBlockedError("amount is not a valid decimal") from exc
    if not parsed.is_finite() or parsed <= ZERO:
        raise IntakeBlockedError("amount must be positive and finite")
    try:
        return positive_money(parsed)
    except MoneyError:
        raise IntakeBlockedError("amount is outside the supported monetary range") from None


def _parse_iso_date(value: object) -> date:
    if not isinstance(value, str):
        raise IntakeBlockedError("due_date must be an ISO date string")
    if _CANONICAL_DATE_RE.fullmatch(value) is None:
        raise IntakeBlockedError("due_date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise IntakeBlockedError("due_date must be YYYY-MM-DD") from exc


def _parse_certainty(value: object) -> Certainty:
    if not isinstance(value, str):
        raise IntakeBlockedError("certainty must be CONFIRMED or UNCERTAIN")
    try:
        return Certainty(value)
    except ValueError as exc:
        raise IntakeBlockedError("certainty must be CONFIRMED or UNCERTAIN") from exc


def _evidence_amounts(source_excerpt: str) -> set[Decimal]:
    amounts: set[Decimal] = set()
    for match in _MONEY_ANCHOR_RE.finditer(source_excerpt):
        token = match.group("amount").replace(",", "")
        try:
            parsed = Decimal(token)
        except InvalidOperation:
            continue
        if parsed.is_finite() and parsed > ZERO:
            amounts.add(parsed)
    return amounts


def _date_anchors(source_excerpt: str) -> list[tuple[date, int]]:
    anchors: list[tuple[date, int]] = []
    for match in _ISO_DATE_ANCHOR_RE.finditer(source_excerpt):
        try:
            anchors.append((date.fromisoformat(match.group("value")), match.start()))
        except ValueError:
            continue
    for match in _LONG_DATE_ANCHOR_RE.finditer(source_excerpt):
        try:
            anchors.append(
                (
                    date(
                        int(match.group("year")),
                        _MONTHS[match.group("month")],
                        int(match.group("day")),
                    ),
                    match.start(),
                )
            )
        except ValueError:
            continue
    return anchors


def _evidence_dates(source_excerpt: str) -> set[date]:
    return {value for value, _ in _date_anchors(source_excerpt)}


def _evidence_due_dates(source_excerpt: str) -> set[date]:
    dates: set[date] = set()
    anchors = _date_anchors(source_excerpt)
    for index, (value, start) in enumerate(anchors):
        prefix = source_excerpt[max(0, start - 96) : start]
        if _NEGATED_DUE_DATE_PREFIX_RE.search(prefix) is not None:
            continue
        if _DUE_DATE_PREFIX_RE.search(prefix) is not None:
            dates.add(value)
            continue
        if index == 0 or _DATE_LIST_CONNECTOR_RE.search(prefix) is None:
            continue
        previous_value, previous_start = anchors[index - 1]
        previous_prefix = source_excerpt[max(0, previous_start - 96) : previous_start]
        if _NEGATED_DUE_DATE_PREFIX_RE.search(previous_prefix) is not None:
            continue
        if _DUE_DATE_PREFIX_RE.search(previous_prefix) is not None:
            dates.add(previous_value)
            dates.add(value)
    return dates


def _require_evidence_value_support(
    source_excerpt: str,
    amount: Decimal | None,
    stated_due_date: date | None,
) -> None:
    if amount is not None and _evidence_amounts(source_excerpt) != {amount}:
        raise IntakeBlockedError("MODEL_EVIDENCE_VALUE_MISMATCH")
    if stated_due_date is not None and _evidence_due_dates(source_excerpt) != {stated_due_date}:
        raise IntakeBlockedError("MODEL_EVIDENCE_VALUE_MISMATCH")


def _candidate_id(
    source_sha256: str,
    name: str,
    amount: Decimal | None,
    stated_due_date: date | None,
    certainty: Certainty,
) -> str:
    amount_token = "null" if amount is None else str(amount)
    date_token = "null" if stated_due_date is None else stated_due_date.isoformat()
    material = f"{source_sha256}|{name}|{amount_token}|{date_token}|{certainty.value}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def parse_and_validate_extraction(
    document: str,
    raw_json: str,
    *,
    as_of: date,
) -> ObligationIntakeResult:
    """Parse one extraction response and conservatively validate obligations."""
    if type(as_of) is not date:
        raise IntakeBlockedError("as_of must be an exact date")
    if len(document) > MAX_DOCUMENT_CHARS:
        raise IntakeBlockedError("document exceeds size limit")
    if len(raw_json) > MAX_MODEL_RESPONSE_CHARS:
        raise IntakeBlockedError("model output exceeds size limit")
    try:
        decoded = json.loads(raw_json, object_pairs_hook=_reject_duplicate_json_keys)
    except _DuplicateJSONKeyError:
        raise IntakeBlockedError("duplicate JSON key") from None
    except json.JSONDecodeError as exc:
        raise IntakeBlockedError("model output is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise IntakeBlockedError("model output must be one JSON object")
    top = cast(dict[str, object], decoded)
    _require_exact_keys(top, _TOP_LEVEL_KEYS, "top-level")
    _require_non_empty_string(
        top["document_summary"], "document_summary", MAX_DOCUMENT_SUMMARY_CHARS
    )
    raw_candidates = top["candidates"]
    if not isinstance(raw_candidates, list):
        raise IntakeBlockedError("candidates must be a list")
    if not raw_candidates:
        raise IntakeBlockedError("ZERO_CANDIDATES_UNVERIFIED")
    if len(raw_candidates) > 20:
        raise IntakeBlockedError("too many candidates")

    normalized_document = _normalized_newlines(document)
    source_sha256 = hashlib.sha256(normalized_document.encode("utf-8")).hexdigest()
    candidates: list[ValidatedCandidate] = []
    obligations: list[Obligation] = []
    seen_candidate_ids: set[str] = set()
    uncertain_reserved_amount = ZERO
    block_reasons: list[str] = []

    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, dict):
            raise IntakeBlockedError("candidate must be a JSON object")
        candidate = cast(dict[str, object], raw_candidate)
        _require_exact_keys(candidate, _CANDIDATE_KEYS, "candidate")

        name = _require_non_empty_string(candidate["name"], "name", MAX_CANDIDATE_NAME_CHARS)
        if candidate["currency"] != "USD":
            raise IntakeBlockedError("currency must be USD")
        certainty = _parse_certainty(candidate["certainty"])

        source_excerpt = _require_non_empty_string(
            candidate["source_excerpt"], "source_excerpt", MAX_SOURCE_EXCERPT_CHARS
        )
        if _normalized_newlines(source_excerpt) not in normalized_document:
            raise IntakeBlockedError("MODEL_EVIDENCE_MISMATCH")

        amount: Decimal | None
        stated_due_date: date | None
        effective_due_date: date | None
        uncertainty_reason: str | None
        if certainty is Certainty.CONFIRMED:
            if candidate["uncertainty_reason"] is not None:
                raise IntakeBlockedError("confirmed candidate cannot have uncertainty_reason")
            uncertainty_reason = None
            amount = _parse_positive_decimal(candidate["amount"])
            stated_due_date = _parse_iso_date(candidate["due_date"])
            effective_due_date = stated_due_date
            reserved_conservatively = False
        else:
            uncertainty_reason = _require_non_empty_string(
                candidate["uncertainty_reason"],
                "uncertainty_reason",
                MAX_UNCERTAINTY_REASON_CHARS,
            )
            amount = (
                None
                if candidate["amount"] is None
                else _parse_positive_decimal(candidate["amount"])
            )
            stated_due_date = (
                None if candidate["due_date"] is None else _parse_iso_date(candidate["due_date"])
            )
            effective_due_date = as_of if amount is not None else None
            reserved_conservatively = amount is not None

        _require_evidence_value_support(source_excerpt, amount, stated_due_date)

        candidate_id = _candidate_id(
            source_sha256,
            name,
            amount,
            stated_due_date,
            certainty,
        )
        if candidate_id in seen_candidate_ids:
            raise IntakeBlockedError("duplicate candidate")
        seen_candidate_ids.add(candidate_id)

        validated = ValidatedCandidate(
            candidate_id=candidate_id,
            name=name,
            amount=amount,
            stated_due_date=stated_due_date,
            effective_due_date=effective_due_date,
            certainty=certainty,
            uncertainty_reason=uncertainty_reason,
            source_excerpt=source_excerpt,
            source_sha256=source_sha256,
            reserved_conservatively=reserved_conservatively,
        )
        candidates.append(validated)

        if amount is None:
            if "UNQUANTIFIED_OBLIGATION" not in block_reasons:
                block_reasons.append("UNQUANTIFIED_OBLIGATION")
            continue

        if effective_due_date is None:
            raise IntakeBlockedError("effective due date missing for quantified obligation")
        obligation = Obligation(
            obligation_id=candidate_id,
            name=name,
            amount=amount,
            due_date=effective_due_date,
        )
        obligations.append(obligation)
        if reserved_conservatively:
            uncertain_reserved_amount += amount

    return ObligationIntakeResult(
        source_sha256=source_sha256,
        candidates=tuple(candidates),
        _effective_obligations=tuple(obligations),
        uncertain_reserved_amount=uncertain_reserved_amount,
        trade_blocked=bool(block_reasons),
        block_reasons=tuple(block_reasons),
    )


def require_effective_obligations(result: ObligationIntakeResult) -> tuple[Obligation, ...]:
    return result.effective_obligations


def confirm_intake_completeness(
    result: ObligationIntakeResult,
    *,
    reviewer_id: str,
) -> ObligationIntakeResult:
    """Explicitly release a safe extraction after human completeness review."""
    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        raise IntakeBlockedError("reviewer_id must be non-empty")
    if result.trade_blocked:
        reasons = ", ".join(result.block_reasons)
        raise IntakeBlockedError(f"intake blocked: {reasons}")
    return replace(
        result,
        completeness_reviewed=True,
        completeness_reviewer_id=reviewer_id.strip(),
    )
