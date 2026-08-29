"""Read-only gateway, adapters, and paper-only environment."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from opaca.broker.adapters import (
    adapt_account,
    adapt_order_snapshot,
    adapt_position,
    map_order_status,
)
from opaca.broker.errors import InvalidBrokerStateError, PaperEnvironmentError
from opaca.broker.gateway import (
    FakeAlpacaGateway,
    assert_read_only_gateway,
    gateway_methods_are_read_only,
)
from opaca.broker.mutation import FORBIDDEN_BROKER_MUTATIONS
from opaca.broker.paper import verify_paper_client
from opaca.domain.models import OrderState
from opaca.domain.money import MoneyError

from tests.helpers import DEFAULT_NOW
from tests.state_helpers import account_payload, order_payload, paper_gateway, position_payload


class TestReadOnlySurface:
    def test_fake_gateway_is_read_only(self) -> None:
        assert gateway_methods_are_read_only(FakeAlpacaGateway)
        gateway = paper_gateway()
        assert_read_only_gateway(gateway)
        for name in FORBIDDEN_BROKER_MUTATIONS:
            assert not callable(getattr(gateway, name, None))

    def test_paper_endpoint_gate_rejects_live(self) -> None:
        class LiveClient:
            _base_url = "https://api.alpaca.markets"
            _paper = False

        with pytest.raises(PaperEnvironmentError):
            verify_paper_client(LiveClient())

    def test_paper_endpoint_gate_accepts_paper(self) -> None:
        class PaperClient:
            _base_url = "https://paper-api.alpaca.markets"
            _paper = True

        assert verify_paper_client(PaperClient()).startswith("https://paper-api.alpaca.markets")


class TestAdapters:
    def test_account_preserves_cash_not_buying_power_as_funding(self) -> None:
        broker = adapt_account(account_payload(cash="99999.99"), DEFAULT_NOW)
        assert broker.cash == Decimal("99999.99")
        assert broker.buying_power == Decimal("400000")
        assert broker.as_of.tzinfo is not None
        assert broker.as_of.utcoffset() is not None

    def test_malformed_account_fails_closed(self) -> None:
        payload = account_payload()
        del payload["cash"]
        with pytest.raises(InvalidBrokerStateError):
            adapt_account(payload, DEFAULT_NOW)

    def test_float_cash_fails_closed(self) -> None:
        payload = account_payload()
        payload["cash"] = 100000.0
        with pytest.raises(InvalidBrokerStateError):
            adapt_account(payload, DEFAULT_NOW)

    def test_malformed_position_fails_closed(self) -> None:
        with pytest.raises((InvalidBrokerStateError, MoneyError, ValueError)):
            adapt_position(
                {"symbol": "SGOV", "qty": "-1", "qty_available": "0", "market_value": "0"}
            )

    def test_short_position_fails_closed(self) -> None:
        with pytest.raises(InvalidBrokerStateError):
            adapt_position(
                {
                    "symbol": "SGOV",
                    "side": "short",
                    "qty": "1",
                    "qty_available": "1",
                    "market_value": "100",
                }
            )

    def test_naive_timestamp_fails_closed(self) -> None:
        with pytest.raises((InvalidBrokerStateError, ValueError)):
            adapt_account(account_payload(), datetime(2026, 9, 1, 14, 30))

    def test_unmapped_order_status_is_unknown(self) -> None:
        assert map_order_status("held") is OrderState.UNKNOWN
        assert map_order_status("totally_new_status") is OrderState.UNKNOWN
        record = adapt_order_snapshot(order_payload("abc", status="new"))
        assert record.mapped_state == OrderState.NEW.value

    def test_malformed_order_fails_closed(self) -> None:
        with pytest.raises(InvalidBrokerStateError):
            adapt_order_snapshot({"symbol": "SGOV", "side": "sell", "status": "new"})

    def test_position_preserves_quantity_available(self) -> None:
        position = adapt_position(position_payload(qty="100", qty_available="40"))
        assert position.quantity == Decimal("100")
        assert position.quantity_available == Decimal("40")
