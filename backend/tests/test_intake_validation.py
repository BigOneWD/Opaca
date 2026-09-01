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
    assert result.effective_obligations == ()
