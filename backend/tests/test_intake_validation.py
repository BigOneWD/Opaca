import json
from datetime import date
from decimal import Decimal

import pytest
from opaca.intake import Certainty, IntakeBlockedError, parse_and_validate_extraction


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
    assert result.effective_obligations[0].amount == Decimal("80000.00")
    assert result.effective_obligations[0].due_date == date(2026, 9, 2)
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
        '"candidates":['
        + candidate
        + ']}'
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
        '"source_excerpt":'
        + json.dumps(document)
        + '}]}'
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
