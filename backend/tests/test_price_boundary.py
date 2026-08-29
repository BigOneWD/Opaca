"""PolicyContext.prices is a hard-control boundary.

Every reference price consumed by TreasuryGuard must already be a strictly
positive finite Decimal within money magnitude limits. Invalid prices are
rejected at construction; missing prices fail closed in CHECK-04 / CHECK-11.
Neither path can reach authority AUTO.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from opaca.domain.models import AuthorityResult, CheckId, Proposal, Side
from opaca.domain.money import MAGNITUDE_LIMIT, MoneyError

from tests.helpers import decide, evaluate, make_context, make_order, make_proposal

PRICE = Decimal("100.00")
VALID_PRICES = {"SGOV": PRICE, "BIL": PRICE, "SHV": PRICE}


def _buy(proposal_id: str) -> Proposal:
    return make_proposal(proposal_id, [make_order(proposal_id, 0, "SGOV", Side.BUY, "1", PRICE)])


class TestValidAndMissingPrices:
    def test_valid_decimal_prices_can_reach_auto(self) -> None:
        context = make_context(prices=VALID_PRICES, obligations=(), operating_reserve=Decimal("0"))
        proposal = _buy("price-ok")
        decision = evaluate(proposal, context)
        assert decision.result_for(CheckId.CHECK_04).passed
        assert decide(proposal, context).result is AuthorityResult.AUTO

    def test_missing_price_fails_closed_and_rejects(self) -> None:
        context = make_context(
            prices={"BIL": PRICE}, obligations=(), operating_reserve=Decimal("0")
        )
        proposal = _buy("price-missing")
        decision = evaluate(proposal, context)
        assert not decision.result_for(CheckId.CHECK_04).passed
        assert not decision.result_for(CheckId.CHECK_11).passed
        assert "fail closed" in decision.result_for(CheckId.CHECK_04).detail
        assert not decision.passed
        assert decide(proposal, context).result is AuthorityResult.REJECT


class TestInvalidPricesRejectedAtBoundary:
    @pytest.mark.parametrize(
        "bad",
        [
            Decimal("0"),
            Decimal("-1.00"),
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
            MAGNITUDE_LIMIT,
        ],
    )
    def test_invalid_decimal_is_rejected(self, bad: Decimal) -> None:
        with pytest.raises(MoneyError):
            make_context(prices={"SGOV": bad, "BIL": PRICE, "SHV": PRICE})

    def test_float_is_not_coerced(self) -> None:
        with pytest.raises(MoneyError):
            make_context(prices={"SGOV": 100.69})  # type: ignore[dict-item]

    def test_bool_is_not_coerced(self) -> None:
        with pytest.raises(MoneyError):
            make_context(prices={"SGOV": True})  # type: ignore[dict-item]

    def test_string_is_not_coerced(self) -> None:
        with pytest.raises(MoneyError):
            make_context(prices={"SGOV": "100.69"})  # type: ignore[dict-item]

    def test_none_is_rejected(self) -> None:
        with pytest.raises(MoneyError):
            make_context(prices={"SGOV": None})  # type: ignore[dict-item]

    def test_invalid_price_cannot_be_evaluated_to_auto(self) -> None:
        with pytest.raises(MoneyError):
            make_context(prices={"SGOV": Decimal("0"), "BIL": PRICE, "SHV": PRICE})
