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
