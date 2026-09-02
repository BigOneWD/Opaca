"""RED-phase contracts for option-specific broker payload adapters."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from opaca.broker.adapters import adapt_position
from opaca.broker.errors import InvalidBrokerStateError
from opaca.wheel.adapters import (
    adapt_option_contract,
    adapt_option_position,
    adapt_option_quote,
)
from opaca.wheel.models import OptionPositionSide, OptionRight

OCC_SYMBOL = "SPY260903P00746000"
NOW = datetime(2026, 9, 2, 14, 17, 55, 762544, tzinfo=UTC)


def contract_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": OCC_SYMBOL,
        "underlying_symbol": "SPY",
        "type": "put",
        "strike_price": "746.0",
        "expiration_date": "2026-09-03",
        "multiplier": "100",
        "status": "active",
        "tradable": True,
    }
    payload.update(overrides)
    return payload


def quote_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": OCC_SYMBOL,
        "bid_price": "0.07",
        "ask_price": "0.08",
        "timestamp": NOW,
    }
    payload.update(overrides)
    return payload


def option_position_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": OCC_SYMBOL,
        "underlying_symbol": "SPY",
        "right": "put",
        "side": "short",
        "qty": "-1",
    }
    payload.update(overrides)
    return payload


def test_valid_option_contract_uses_authoritative_identity_and_multiplier() -> None:
    contract = adapt_option_contract(contract_payload())

    assert contract.occ_symbol == OCC_SYMBOL
    assert contract.underlying == "SPY"
    assert contract.right is OptionRight.PUT
    assert contract.strike == Decimal("746.0")
    assert contract.expiration == date(2026, 9, 3)
    assert contract.multiplier == Decimal("100")
    assert contract.active is True
    assert contract.tradable is True


@pytest.mark.parametrize("multiplier", [None, "0", "-1"])
def test_missing_or_non_positive_multiplier_fails_closed(multiplier: object) -> None:
    payload = contract_payload(multiplier=multiplier)
    if multiplier is None:
        del payload["multiplier"]

    with pytest.raises(InvalidBrokerStateError):
        adapt_option_contract(payload)


def test_malformed_strike_fails_closed() -> None:
    with pytest.raises(InvalidBrokerStateError):
        adapt_option_contract(contract_payload(strike_price="not-a-price"))


@pytest.mark.parametrize("field", ["symbol", "underlying_symbol"])
def test_missing_contract_identity_fails_closed(field: str) -> None:
    payload = contract_payload()
    del payload[field]

    with pytest.raises(InvalidBrokerStateError):
        adapt_option_contract(payload)


def test_non_put_contract_fails_closed() -> None:
    with pytest.raises(InvalidBrokerStateError):
        adapt_option_contract(contract_payload(type="call"))


def test_option_quote_preserves_decimal_prices_and_aware_timestamp() -> None:
    quote = adapt_option_quote(quote_payload())

    assert quote.bid == Decimal("0.07")
    assert quote.ask == Decimal("0.08")
    assert quote.as_of == NOW
    assert quote.as_of.tzinfo is UTC


def test_naive_option_quote_timestamp_fails_closed() -> None:
    with pytest.raises(InvalidBrokerStateError):
        adapt_option_quote(quote_payload(timestamp=datetime(2026, 9, 2, 14, 17, 55)))


@pytest.mark.parametrize("field", ["bid_price", "ask_price"])
def test_malformed_option_quote_number_fails_closed(field: str) -> None:
    with pytest.raises(InvalidBrokerStateError):
        adapt_option_quote(quote_payload(**{field: "not-a-price"}))


def test_short_option_position_adapts_without_changing_equity_short_policy() -> None:
    position = adapt_option_position(option_position_payload())

    assert position.occ_symbol == OCC_SYMBOL
    assert position.underlying == "SPY"
    assert position.right is OptionRight.PUT
    assert position.side is OptionPositionSide.SHORT
    assert position.contracts == 1

    with pytest.raises(InvalidBrokerStateError):
        adapt_position(
            {
                "symbol": "SPY",
                "side": "short",
                "qty": "1",
                "qty_available": "1",
                "market_value": "74600",
            }
        )


@pytest.mark.parametrize("quantity", ["0", "not-a-quantity"])
def test_malformed_or_zero_option_quantity_fails_closed(quantity: str) -> None:
    with pytest.raises(InvalidBrokerStateError):
        adapt_option_position(option_position_payload(qty=quantity))
