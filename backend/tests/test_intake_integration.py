from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from opaca.domain.models import BrokerCashState
from opaca.intake import (
    IntakeBlockedError,
    confirm_intake_completeness,
    parse_and_validate_extraction,
    require_effective_obligations,
)
from opaca.treasury.liquidity import compute_liquidity


def _broker(cash: Decimal) -> BrokerCashState:
    return BrokerCashState(
        cash=cash,
        buying_power=Decimal("4000000"),
        non_marginable_buying_power=cash,
        multiplier=Decimal("4"),
        as_of=datetime(2026, 9, 2, 0, 30, tzinfo=UTC),
    )


def test_confirmed_and_uncertain_obligations_reduce_investable_cash() -> None:
    document = (
        "September payroll: Payment of USD 240,000.00 is due by 12 September 2026.\n"
        "Vendor invoice total: USD 80,000. Terms: net 30 from receipt."
    )
    raw = """{
      "document_summary": "Payroll and vendor obligations",
      "candidates": [
        {
          "name": "September payroll",
          "amount": "240000.00",
          "due_date": "2026-09-12",
          "currency": "USD",
          "certainty": "CONFIRMED",
          "uncertainty_reason": null,
          "source_excerpt": "Payment of USD 240,000.00 is due by 12 September 2026."
        },
        {
          "name": "Vendor invoice",
          "amount": "80000.00",
          "due_date": null,
          "currency": "USD",
          "certainty": "UNCERTAIN",
          "uncertainty_reason": "Receipt date is not stated",
          "source_excerpt": "Vendor invoice total: USD 80,000. Terms: net 30 from receipt."
        }
      ]
    }"""

    intake = parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))
    reviewed_intake = confirm_intake_completeness(intake, reviewer_id="treasury-reviewer")
    obligations = require_effective_obligations(reviewed_intake)
    projection = compute_liquidity(
        broker=_broker(Decimal("1000000")),
        obligations=obligations,
        settlement_events=(),
        operating_reserve=Decimal("400000"),
        as_of=date(2026, 9, 2),
    )

    assert intake.trade_blocked is False
    assert intake.uncertain_reserved_amount == Decimal("80000.00")
    assert projection.obligations_total == Decimal("320000.00")
    assert projection.protected_liquidity == Decimal("720000.00")
    assert projection.investable_cash == Decimal("280000.00")
    assert projection.funding_ceiling == Decimal("280000.00")
    assert projection.obligations[1].due_date == date(2026, 9, 2)


def test_unquantified_obligation_cannot_be_handed_to_liquidity_engine() -> None:
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

    intake = parse_and_validate_extraction(document, raw, as_of=date(2026, 9, 2))

    assert intake.trade_blocked is True
    with pytest.raises(IntakeBlockedError, match="UNQUANTIFIED_OBLIGATION"):
        _ = intake.effective_obligations
    with pytest.raises(IntakeBlockedError, match="UNQUANTIFIED_OBLIGATION"):
        require_effective_obligations(intake)
