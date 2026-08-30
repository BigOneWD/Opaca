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
from opaca.broker.alpaca import AlpacaPaperGateway
from opaca.broker.errors import InvalidBrokerStateError, PaperEnvironmentError
from opaca.broker.gateway import (
    FakeAlpacaGateway,
    ReadOnlyAlpacaGateway,
    assert_read_only_gateway,
    gateway_methods_are_read_only,
    public_gateway_methods,
)
from opaca.broker.mutation import ALLOWED_GATEWAY_METHODS, FORBIDDEN_BROKER_MUTATIONS
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

    def test_forbidden_names_include_alpaca_py_mutators(self) -> None:
        assert "cancel_order_by_id" in FORBIDDEN_BROKER_MUTATIONS
        assert "replace_order_by_id" in FORBIDDEN_BROKER_MUTATIONS
        assert "submit_order" in FORBIDDEN_BROKER_MUTATIONS
        assert "close_all_positions" in FORBIDDEN_BROKER_MUTATIONS

    def test_paper_gateway_does_not_retain_trading_client(self) -> None:
        class PaperReadClient:
            _base_url = "https://paper-api.alpaca.markets"
            _paper = True

            def get_account(self) -> dict[str, object]:
                return {}

            def get_all_positions(self) -> tuple[object, ...]:
                return ()

            def get_asset(self, symbol: str) -> dict[str, object]:
                return {"symbol": symbol}

            def get_orders(self, filter: object = None) -> tuple[object, ...]:
                return ()

            def get_order_by_client_id(self, client_order_id: str) -> dict[str, object]:
                return {"client_order_id": client_order_id}

            def get_calendar(self, filters: object = None) -> tuple[object, ...]:
                return ()

            def get_clock(self) -> dict[str, object]:
                return {}

            def submit_order(self, *args: object, **kwargs: object) -> str:
                raise AssertionError("must never be reachable via the gateway")

        gateway = AlpacaPaperGateway(PaperReadClient())
        assert_read_only_gateway(gateway)
        assert gateway_methods_are_read_only(AlpacaPaperGateway)
        assert public_gateway_methods(AlpacaPaperGateway) == ALLOWED_GATEWAY_METHODS
        assert not hasattr(gateway, "_client")
        assert getattr(gateway, "_client", None) is None
        assert getattr(gateway, "client", None) is None
        for name in FORBIDDEN_BROKER_MUTATIONS:
            assert not callable(getattr(gateway, name, None))

    def test_nested_mutable_client_is_rejected(self) -> None:
        class Mutable:
            def submit_order(self, *args: object, **kwargs: object) -> str:
                return "SUBMITTED"

        gateway = paper_gateway()
        gateway._client = Mutable()  # type: ignore[attr-defined]
        with pytest.raises(InvalidBrokerStateError):
            assert_read_only_gateway(gateway)

    def test_cancel_order_by_id_on_gateway_is_rejected(self) -> None:
        class Sneaky(FakeAlpacaGateway):
            def cancel_order_by_id(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("must never be invoked")

        with pytest.raises(InvalidBrokerStateError):
            assert_read_only_gateway(Sneaky(account={}, assets={}))

    def test_protocol_is_read_only_capability(self) -> None:
        assert ReadOnlyAlpacaGateway is not None
        names = {name for name in dir(ReadOnlyAlpacaGateway) if not name.startswith("_")}
        assert names & FORBIDDEN_BROKER_MUTATIONS == set()

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

    def test_filled_exceeds_quantity_fails_closed(self) -> None:
        with pytest.raises(InvalidBrokerStateError):
            adapt_order_snapshot(order_payload("abc", qty="10", filled_qty="50"))

    def test_position_preserves_quantity_available(self) -> None:
        position = adapt_position(position_payload(qty="100", qty_available="40"))
        assert position.quantity == Decimal("100")
        assert position.quantity_available == Decimal("40")
