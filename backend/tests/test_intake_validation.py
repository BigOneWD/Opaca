import json
from datetime import date, datetime
from decimal import Decimal

import pytest
from opaca.intake import (
    Certainty,
    IntakeBlockedError,
    confirm_intake_completeness,
    parse_and_validate_extraction,
    require_effective_obligations,
)


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
    reviewed = confirm_intake_completeness(result, reviewer_id="test-reviewer")
    assert reviewed.effective_obligations[0].amount == Decimal("240000.00")


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
    assert candidate.certainty is Certainty.UNCERTAIN
    assert candidate.amount == Decimal("80000.00")
    assert candidate.stated_due_date is None
    assert candidate.effective_due_date == date(2026, 9, 2)
    assert candidate.reserved_conservatively is True
    assert result.uncertain_reserved_amount == Decimal("80000.00")
    reviewed = confirm_intake_completeness(result, reviewer_id="test-reviewer")
    assert reviewed.effective_obligations[0].amount == Decimal("80000.00")
    assert reviewed.effective_obligations[0].due_date == date(2026, 9, 2)
    assert result.trade_blocked is False


def test_uncertain_known_amount_must_be_supported_by_exact_evidence() -> None:
    document = "Invoice total: USD 80,000. Terms: net 30 from receipt."
    raw = json.dumps(
        {
            "document_summary": "Vendor invoice",
            "candidates": [
                {
                    "name": "Vendor invoice",
                    "amount": "1.00",
                    "due_date": None,
                    "currency": "USD",
                    "certainty": "UNCERTAIN",
                    "uncertainty_reason": "Receipt date is not stated",
                    "source_excerpt": document,
                }
            ],
        }
    )

    with pytest.raises(IntakeBlockedError, match="MODEL_EVIDENCE_VALUE_MISMATCH"):
        parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))


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
    with pytest.raises(IntakeBlockedError, match="UNQUANTIFIED_OBLIGATION"):
        _ = result.effective_obligations


def test_zero_candidates_never_becomes_safe_empty_obligations() -> None:
    document = "Quarterly tax payment of USD 500,000.00 is due by 30 September 2026."
    raw = json.dumps(
        {
            "document_summary": "Quarterly tax notice",
            "candidates": [],
        }
    )

    with pytest.raises(IntakeBlockedError, match="ZERO_CANDIDATES_UNVERIFIED"):
        parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))


def test_oversized_fixture_response_is_blocked() -> None:
    document = "A regulatory payment is due this month; amount pending assessment."
    raw = json.dumps(
        {
            "document_summary": "unquantified note",
            "candidates": [
                {
                    "name": "unquantified note",
                    "amount": None,
                    "due_date": None,
                    "currency": "USD",
                    "certainty": "UNCERTAIN",
                    "uncertainty_reason": "Amount is not stated",
                    "source_excerpt": document,
                }
            ],
        }
    ) + (" " * 100_001)

    with pytest.raises(IntakeBlockedError, match="model output exceeds"):
        parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))


def test_oversized_fixture_document_is_blocked() -> None:
    document = "x" * 50_001
    raw = json.dumps(
        {
            "document_summary": "unquantified note",
            "candidates": [
                {
                    "name": "unquantified note",
                    "amount": None,
                    "due_date": None,
                    "currency": "USD",
                    "certainty": "UNCERTAIN",
                    "uncertainty_reason": "Amount is not stated",
                    "source_excerpt": document,
                }
            ],
        }
    )

    with pytest.raises(IntakeBlockedError, match="document exceeds"):
        parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("document_summary", 2_000),
        ("name", 500),
        ("uncertainty_reason", 2_000),
        ("source_excerpt", 10_000),
    ],
)
def test_oversized_candidate_scalar_is_blocked(field: str, limit: int) -> None:
    document = "Payment of USD 10.00 is due by 12 September 2026."
    candidate: dict[str, object] = {
        "name": "payment",
        "amount": "10.00",
        "due_date": "2026-09-12",
        "currency": "USD",
        "certainty": "CONFIRMED",
        "uncertainty_reason": None,
        "source_excerpt": document,
    }
    if field in {"uncertainty_reason", "source_excerpt"}:
        candidate["certainty"] = "UNCERTAIN"
        candidate["amount"] = None
        candidate["due_date"] = None
        candidate["uncertainty_reason"] = "Reason is stated"
    payload: dict[str, object] = {
        "document_summary": "payment",
        "candidates": [candidate],
    }
    if field == "document_summary":
        payload[field] = "x" * (limit + 1)
    elif field == "source_excerpt":
        document = "x" * (limit + 1)
        candidate[field] = document
    else:
        candidate[field] = "x" * (limit + 1)

    with pytest.raises(IntakeBlockedError, match="length limit"):
        parse_and_validate_extraction(document, json.dumps(payload), as_of=date(2026, 9, 2))


def test_duplicate_top_level_json_key_is_rejected() -> None:
    document = "Payment of USD 10.00 is due by 12 September 2026."
    candidate = json.dumps(
        {
            "name": "payment",
            "amount": "10.00",
            "due_date": "2026-09-12",
            "currency": "USD",
            "certainty": "CONFIRMED",
            "uncertainty_reason": None,
            "source_excerpt": document,
        }
    )
    raw = (
        '{"document_summary":"payment",'
        '"document_summary":"overwritten payment",'
        '"candidates":[' + candidate + "]}"
    )

    with pytest.raises(IntakeBlockedError, match="duplicate JSON key"):
        parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))


def test_duplicate_candidate_json_key_is_rejected() -> None:
    document = "Payment of USD 10.00 is due by 12 September 2026."
    raw = (
        '{"document_summary":"payment","candidates":[{'
        '"name":"payment",'
        '"name":"overwritten payment",'
        '"amount":"10.00",'
        '"due_date":"2026-09-12",'
        '"currency":"USD",'
        '"certainty":"CONFIRMED",'
        '"uncertainty_reason":null,'
        '"source_excerpt":' + json.dumps(document) + "}]}"
    )

    with pytest.raises(IntakeBlockedError, match="duplicate JSON key"):
        parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))


@pytest.mark.parametrize("due_date", ["20260912", "2026-W37-6"])
def test_noncanonical_due_date_is_rejected(due_date: str) -> None:
    document = "Payment of USD 10.00 is due by 12 September 2026."
    raw = json.dumps(
        {
            "document_summary": "payment",
            "candidates": [
                {
                    "name": "payment",
                    "amount": "10.00",
                    "due_date": due_date,
                    "currency": "USD",
                    "certainty": "CONFIRMED",
                    "uncertainty_reason": None,
                    "source_excerpt": document,
                }
            ],
        }
    )

    with pytest.raises(IntakeBlockedError, match="YYYY-MM-DD"):
        parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))


@pytest.mark.parametrize(
    "certainty",
    ["Certainty.CONFIRMED", "foo.confirmed", "confirmed", ""],
)
def test_noncanonical_certainty_is_rejected(certainty: str) -> None:
    document = "Payment of USD 10.00 is due by 12 September 2026."
    raw = json.dumps(
        {
            "document_summary": "payment",
            "candidates": [
                {
                    "name": "payment",
                    "amount": "10.00",
                    "due_date": "2026-09-12",
                    "currency": "USD",
                    "certainty": certainty,
                    "uncertainty_reason": None,
                    "source_excerpt": document,
                }
            ],
        }
    )

    with pytest.raises(IntakeBlockedError):
        parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))


def test_evidence_mismatch_blocks_entire_run() -> None:
    document = "Payment of USD 10.00 is due by 12 September 2026."
    raw = """{
      "document_summary": "payment",
      "candidates": [{
        "name": "payment",
        "amount": "10.00",
        "due_date": "2026-09-12",
        "currency": "USD",
        "certainty": "CONFIRMED",
        "uncertainty_reason": null,
        "source_excerpt": "Payment of USD 999.00 is due tomorrow."
      }]
    }"""

    with pytest.raises(IntakeBlockedError, match="MODEL_EVIDENCE_MISMATCH"):
        parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))


@pytest.mark.parametrize(
    ("field", "unsupported_value"),
    [
        ("amount", "1.00"),
        ("due_date", "2026-12-31"),
    ],
)
def test_confirmed_amount_and_due_date_must_be_supported_by_exact_evidence(
    field: str,
    unsupported_value: str,
) -> None:
    document = "Payment of USD 240,000.00 is due by 12 September 2026."
    candidate: dict[str, object] = {
        "name": "September payroll",
        "amount": "240000.00",
        "due_date": "2026-09-12",
        "currency": "USD",
        "certainty": "CONFIRMED",
        "uncertainty_reason": None,
        "source_excerpt": document,
    }
    candidate[field] = unsupported_value
    raw = json.dumps(
        {
            "document_summary": "September payroll",
            "candidates": [candidate],
        }
    )

    with pytest.raises(IntakeBlockedError, match="MODEL_EVIDENCE_VALUE_MISMATCH"):
        parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))


def test_confirmed_due_date_must_be_tied_to_due_semantics() -> None:
    document = "Invoice reference 2026-12-31. Payment of USD 100.00 is due next quarter."
    raw = json.dumps(
        {
            "document_summary": "payment",
            "candidates": [
                {
                    "name": "payment",
                    "amount": "100.00",
                    "due_date": "2026-12-31",
                    "currency": "USD",
                    "certainty": "CONFIRMED",
                    "uncertainty_reason": None,
                    "source_excerpt": document,
                }
            ],
        }
    )

    with pytest.raises(IntakeBlockedError, match="MODEL_EVIDENCE_VALUE_MISMATCH"):
        parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))


def test_invoice_only_date_cannot_be_used_when_explicit_due_date_is_elsewhere() -> None:
    document = "Invoice date 2026-09-01. Invoice total USD 100.00. Payment is due on 2026-09-30."
    raw = json.dumps(
        {
            "document_summary": "payment",
            "candidates": [
                {
                    "name": "payment",
                    "amount": "100.00",
                    "due_date": "2026-09-01",
                    "currency": "USD",
                    "certainty": "CONFIRMED",
                    "uncertainty_reason": None,
                    "source_excerpt": "Invoice date 2026-09-01. Invoice total USD 100.00.",
                }
            ],
        }
    )

    with pytest.raises(IntakeBlockedError, match="MODEL_EVIDENCE_VALUE_MISMATCH"):
        parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))


def test_explicit_due_date_and_payable_date_semantics_are_accepted() -> None:
    cases = (
        ("Invoice total USD 100.00. Due date: 2026-12-31.", "2026-12-31"),
        ("Invoice total USD 100.00. Payable on 2026-12-31.", "2026-12-31"),
    )

    for document, due_date in cases:
        result = parse_and_validate_extraction(
            document,
            json.dumps(
                {
                    "document_summary": "payment",
                    "candidates": [
                        {
                            "name": "payment",
                            "amount": "100.00",
                            "due_date": due_date,
                            "currency": "USD",
                            "certainty": "CONFIRMED",
                            "uncertainty_reason": None,
                            "source_excerpt": document,
                        }
                    ],
                }
            ),
            as_of=date(2026, 9, 2),
        )
        assert result.trade_blocked is False


def test_explicit_due_date_is_selected_over_unrelated_invoice_date() -> None:
    document = "Invoice date 2026-09-01. Invoice total USD 100.00. Payment is due on 2026-09-30."
    raw = json.dumps(
        {
            "document_summary": "payment",
            "candidates": [
                {
                    "name": "payment",
                    "amount": "100.00",
                    "due_date": "2026-09-30",
                    "currency": "USD",
                    "certainty": "CONFIRMED",
                    "uncertainty_reason": None,
                    "source_excerpt": document,
                }
            ],
        }
    )

    result = parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))

    assert result.trade_blocked is False


def test_multiple_due_dates_remain_ambiguous() -> None:
    document = "Payment of USD 100.00 is due on 2026-09-12 or 2026-09-30."
    raw = json.dumps(
        {
            "document_summary": "payment",
            "candidates": [
                {
                    "name": "payment",
                    "amount": "100.00",
                    "due_date": "2026-09-12",
                    "currency": "USD",
                    "certainty": "CONFIRMED",
                    "uncertainty_reason": None,
                    "source_excerpt": document,
                }
            ],
        }
    )

    with pytest.raises(IntakeBlockedError, match="MODEL_EVIDENCE_VALUE_MISMATCH"):
        parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))


@pytest.mark.parametrize(
    "as_of",
    ["2026-09-02", 123, datetime(2026, 9, 2), None],
)
def test_runtime_as_of_must_be_an_exact_date(as_of: object) -> None:
    document = "Payment of USD 10.00 has an unresolved due date."
    raw = json.dumps(
        {
            "document_summary": "payment",
            "candidates": [
                {
                    "name": "payment",
                    "amount": "10.00",
                    "due_date": None,
                    "currency": "USD",
                    "certainty": "UNCERTAIN",
                    "uncertainty_reason": "Due date is not stated",
                    "source_excerpt": document,
                }
            ],
        }
    )

    with pytest.raises(IntakeBlockedError, match="as_of must be an exact date"):
        parse_and_validate_extraction(document, raw, as_of=as_of)  # type: ignore[arg-type]


def test_float_amount_is_rejected() -> None:
    document = "Payment of USD 10.00 is due by 12 September 2026."
    raw = json.dumps(
        {
            "document_summary": "payment",
            "candidates": [
                {
                    "name": "payment",
                    "amount": 10.0,
                    "due_date": "2026-09-12",
                    "currency": "USD",
                    "certainty": "CONFIRMED",
                    "uncertainty_reason": None,
                    "source_excerpt": document,
                }
            ],
        }
    )

    with pytest.raises(IntakeBlockedError, match="decimal string"):
        parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))


def test_oversized_amount_becomes_intake_blocked() -> None:
    amount = "100000000000000000000000000"
    document = f"Payment of USD {amount} is due by 12 September 2026."
    raw = json.dumps(
        {
            "document_summary": "oversized payment",
            "candidates": [
                {
                    "name": "oversized payment",
                    "amount": amount,
                    "due_date": "2026-09-12",
                    "currency": "USD",
                    "certainty": "CONFIRMED",
                    "uncertainty_reason": None,
                    "source_excerpt": document,
                }
            ],
        }
    )

    with pytest.raises(IntakeBlockedError, match="amount"):
        parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))


def test_extra_candidate_key_is_rejected() -> None:
    document = "Payment of USD 10.00 is due by 12 September 2026."
    raw = json.dumps(
        {
            "document_summary": "payment",
            "candidates": [
                {
                    "name": "payment",
                    "amount": "10.00",
                    "due_date": "2026-09-12",
                    "currency": "USD",
                    "certainty": "CONFIRMED",
                    "uncertainty_reason": None,
                    "source_excerpt": document,
                    "broker_action": "BUY",
                }
            ],
        }
    )

    with pytest.raises(IntakeBlockedError, match="schema keys"):
        parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))


def test_more_than_20_candidates_is_rejected() -> None:
    document = "Payment of USD 10.00 is due by 12 September 2026."
    candidate: dict[str, object] = {
        "name": "payment",
        "amount": "10.00",
        "due_date": "2026-09-12",
        "currency": "USD",
        "certainty": "CONFIRMED",
        "uncertainty_reason": None,
        "source_excerpt": document,
    }
    raw = json.dumps(
        {
            "document_summary": "payment",
            "candidates": [dict(candidate) for _ in range(21)],
        }
    )

    with pytest.raises(IntakeBlockedError, match="too many candidates"):
        parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))


def test_duplicate_normalized_candidate_is_rejected() -> None:
    document = "Payment of USD 10.00 is due by 12 September 2026."
    candidate: dict[str, object] = {
        "name": "payment",
        "amount": "10.00",
        "due_date": "2026-09-12",
        "currency": "USD",
        "certainty": "CONFIRMED",
        "uncertainty_reason": None,
        "source_excerpt": document,
    }
    raw = json.dumps(
        {
            "document_summary": "payment",
            "candidates": [dict(candidate), dict(candidate)],
        }
    )

    with pytest.raises(IntakeBlockedError, match="duplicate"):
        parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))


def test_unreviewed_partial_extraction_cannot_be_handed_off() -> None:
    document = (
        "Principal invoice total: USD 1,000.00 is due by 2026-09-12. "
        "Filing fee: USD 10.00 is due by 2026-09-12."
    )
    raw = json.dumps(
        {
            "document_summary": "Filing fee only",
            "candidates": [
                {
                    "name": "Filing fee",
                    "amount": "10.00",
                    "due_date": "2026-09-12",
                    "currency": "USD",
                    "certainty": "CONFIRMED",
                    "uncertainty_reason": None,
                    "source_excerpt": "Filing fee: USD 10.00 is due by 2026-09-12.",
                }
            ],
        }
    )

    result = parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))

    with pytest.raises(IntakeBlockedError, match="COMPLETENESS_REVIEW_REQUIRED"):
        require_effective_obligations(result)


def test_even_valid_complete_extraction_requires_explicit_completeness_review() -> None:
    document = "Payment of USD 10.00 is due by 12 September 2026."
    raw = json.dumps(
        {
            "document_summary": "payment",
            "candidates": [
                {
                    "name": "payment",
                    "amount": "10.00",
                    "due_date": "2026-09-12",
                    "currency": "USD",
                    "certainty": "CONFIRMED",
                    "uncertainty_reason": None,
                    "source_excerpt": document,
                }
            ],
        }
    )

    result = parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))

    with pytest.raises(IntakeBlockedError, match="COMPLETENESS_REVIEW_REQUIRED"):
        _ = result.effective_obligations


def test_explicit_completeness_review_allows_safe_handoff_without_changing_extraction() -> None:
    document = "Payment of USD 10.00 is due by 12 September 2026."
    raw = json.dumps(
        {
            "document_summary": "payment",
            "candidates": [
                {
                    "name": "payment",
                    "amount": "10.00",
                    "due_date": "2026-09-12",
                    "currency": "USD",
                    "certainty": "CONFIRMED",
                    "uncertainty_reason": None,
                    "source_excerpt": document,
                }
            ],
        }
    )
    result = parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))

    reviewed = confirm_intake_completeness(result, reviewer_id="treasury-reviewer")

    assert reviewed.completeness_reviewed is True
    assert reviewed.completeness_reviewer_id == "treasury-reviewer"
    assert reviewed.candidates == result.candidates
    assert reviewed.effective_obligations == result._effective_obligations


def test_blocked_intake_cannot_be_approved_for_completeness() -> None:
    document = "A regulatory payment is due this month; amount pending assessment."
    raw = json.dumps(
        {
            "document_summary": "Regulatory payment",
            "candidates": [
                {
                    "name": "Regulatory payment",
                    "amount": None,
                    "due_date": None,
                    "currency": "USD",
                    "certainty": "UNCERTAIN",
                    "uncertainty_reason": "Amount and exact date are not stated",
                    "source_excerpt": document,
                }
            ],
        }
    )
    result = parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))

    with pytest.raises(IntakeBlockedError, match="UNQUANTIFIED_OBLIGATION"):
        confirm_intake_completeness(result, reviewer_id="treasury-reviewer")


def test_completeness_review_requires_non_empty_reviewer_id() -> None:
    document = "Payment of USD 10.00 is due by 12 September 2026."
    raw = json.dumps(
        {
            "document_summary": "payment",
            "candidates": [
                {
                    "name": "payment",
                    "amount": "10.00",
                    "due_date": "2026-09-12",
                    "currency": "USD",
                    "certainty": "CONFIRMED",
                    "uncertainty_reason": None,
                    "source_excerpt": document,
                }
            ],
        }
    )
    result = parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))

    with pytest.raises(IntakeBlockedError, match="reviewer_id"):
        confirm_intake_completeness(result, reviewer_id=" ")
