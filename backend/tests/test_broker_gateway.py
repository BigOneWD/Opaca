"""Read-only gateway, adapters, and paper-only environment."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

import pytest
from opaca.broker.adapters import (
    adapt_account,
    adapt_asset,
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
from opaca.domain.models import AssetStatus, OrderState
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

        assert verify_paper_client(PaperClient()) == "https://paper-api.alpaca.markets"


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

    def test_real_alpaca_order_enums_are_adapted(self) -> None:
        from alpaca.trading.enums import OrderSide, OrderStatus

        record = adapt_order_snapshot(
            {
                "id": "2cc3948e-c36a-4f61-8d30-6963a8d543ff",
                "client_order_id": "opaca-38f63d11f149c3e17c4f34df48e9dc8d",
                "symbol": "SGOV",
                "side": OrderSide.BUY,
                "status": OrderStatus.FILLED,
                "qty": "1",
                "filled_qty": "1",
            }
        )

        assert record.side == "BUY"
        assert record.alpaca_status == "filled"
        assert record.mapped_state == OrderState.FILLED.value

        payload = order_payload("new")
        payload["side"] = OrderSide.SELL
        payload["status"] = OrderStatus.NEW
        new_record = adapt_order_snapshot(payload)
        assert new_record.side == "SELL"
        assert new_record.alpaca_status == "new"
        assert new_record.mapped_state == OrderState.NEW.value

    @pytest.mark.parametrize(
        ("side", "status", "expected_side", "expected_status", "expected_state"),
        [
            ("buy", "filled", "BUY", "filled", OrderState.FILLED.value),
            ("BUY", "FILLED", "BUY", "filled", OrderState.FILLED.value),
            ("sell", "new", "SELL", "new", OrderState.NEW.value),
            ("SELL", "NEW", "SELL", "new", OrderState.NEW.value),
        ],
    )
    def test_plain_order_tokens_are_normalized(
        self,
        side: str,
        status: str,
        expected_side: str,
        expected_status: str,
        expected_state: str,
    ) -> None:
        record = adapt_order_snapshot(order_payload("plain", side=side, status=status))

        assert record.side == expected_side
        assert record.alpaca_status == expected_status
        assert record.mapped_state == expected_state

    @pytest.mark.parametrize("side", ["OrderSide.BUY", "foo.buy", "not_buy", "", None, object()])
    def test_invalid_order_side_fails_closed(self, side: object) -> None:
        payload = order_payload("bad-side")
        payload["side"] = side

        with pytest.raises(InvalidBrokerStateError):
            adapt_order_snapshot(payload)

    @pytest.mark.parametrize("status", ["OrderStatus.FILLED", "foo.filled", "not_filled"])
    def test_unknown_order_status_stays_unknown(self, status: str) -> None:
        payload = order_payload("unknown-status")
        payload["status"] = status

        record = adapt_order_snapshot(payload)
        assert record.mapped_state == OrderState.UNKNOWN.value
        assert record.alpaca_status == status.lower()

    @pytest.mark.parametrize("status", ["", None, object()])
    def test_invalid_order_status_fails_closed(self, status: object) -> None:
        payload = order_payload("invalid-status")
        payload["status"] = status

        with pytest.raises(InvalidBrokerStateError):
            adapt_order_snapshot(payload)

    @pytest.mark.parametrize("field", ["side", "status"])
    def test_missing_order_enum_field_fails_closed(self, field: str) -> None:
        payload = order_payload("missing-field")
        del payload[field]

        with pytest.raises(InvalidBrokerStateError):
            adapt_order_snapshot(payload)

    def test_position_preserves_quantity_available(self) -> None:
        position = adapt_position(position_payload(qty="100", qty_available="40"))
        assert position.quantity == Decimal("100")
        assert position.quantity_available == Decimal("40")

    def test_real_alpaca_long_position_enum_is_adapted(self) -> None:
        from alpaca.trading.enums import PositionSide

        position = adapt_position(
            {
                "symbol": "SGOV",
                "side": PositionSide.LONG,
                "qty": "1",
                "qty_available": "1",
                "market_value": "100.4023",
            }
        )

        assert position.symbol == "SGOV"
        assert position.quantity == Decimal("1")
        assert position.quantity_available == Decimal("1")
        assert position.market_value == Decimal("100.4023")

    def test_alpaca_short_position_enum_fails_closed(self) -> None:
        from alpaca.trading.enums import PositionSide

        payload = position_payload(qty="1", qty_available="1", market_value="100.4023")
        payload["side"] = PositionSide.SHORT

        with pytest.raises(InvalidBrokerStateError):
            adapt_position(payload)

    @pytest.mark.parametrize(
        "side",
        [
            "short",
            "SHORT",
            "PositionSide.LONG",
            "foo.long",
            "not_long",
            AssetStatus.ACTIVE,
            object(),
        ],
    )
    def test_invalid_supplied_position_side_fails_closed(self, side: object) -> None:
        payload = position_payload(qty="1", qty_available="1", market_value="100.4023")
        payload["side"] = side

        with pytest.raises(InvalidBrokerStateError):
            adapt_position(payload)

    def test_missing_position_side_remains_optional(self) -> None:
        payload = position_payload(qty="1", qty_available="1", market_value="100.4023")
        del payload["side"]

        position = adapt_position(payload)
        assert position.quantity == Decimal("1")

    def test_none_position_side_remains_optional(self) -> None:
        payload = position_payload(qty="1", qty_available="1", market_value="100.4023")
        payload["side"] = None

        position = adapt_position(payload)
        assert position.quantity == Decimal("1")


class _EnumLike:
    def __init__(self, value: str, *, name: str | None = None) -> None:
        self.value = value
        if name is not None:
            self.name = name

    def __str__(self) -> str:
        return f"AssetStatus.{self.value}"


class _ContainsActive:
    def __str__(self) -> str:
        return "AssetStatus.ACTIVE"


class BrokerAssetStatus(Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


def _asset_payload(
    *,
    symbol: str = "SGOV",
    status: object = "active",
    tradable: object = True,
    fractionable: object = True,
    include_status: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": symbol,
        "tradable": tradable,
        "fractionable": fractionable,
    }
    if include_status:
        payload["status"] = status
    return payload


class TestAdaptAsset:
    def test_plain_active_string(self) -> None:
        state = adapt_asset(_asset_payload(status="active"))
        assert state.status is AssetStatus.ACTIVE
        assert state.symbol == "SGOV"

    def test_plain_active_uppercase(self) -> None:
        state = adapt_asset(_asset_payload(status="ACTIVE"))
        assert state.status is AssetStatus.ACTIVE

    def test_python_enum_active(self) -> None:
        state = adapt_asset(_asset_payload(status=BrokerAssetStatus.ACTIVE))
        assert state.status is AssetStatus.ACTIVE

    def test_alpaca_py_asset_status_active(self) -> None:
        from alpaca.trading.enums import AssetStatus as AlpacaAssetStatus

        state = adapt_asset(_asset_payload(status=AlpacaAssetStatus.ACTIVE))
        assert state.status is AssetStatus.ACTIVE
        assert str(AlpacaAssetStatus.ACTIVE).lower() == "assetstatus.active"

    def test_plain_inactive_is_inactive_not_active(self) -> None:
        state = adapt_asset(_asset_payload(status="inactive"))
        assert state.status is AssetStatus.INACTIVE
        assert state.status.value == "inactive"

    def test_enum_like_inactive_is_never_active(self) -> None:
        state = adapt_asset(_asset_payload(status=BrokerAssetStatus.INACTIVE))
        assert state.status is AssetStatus.INACTIVE
        assert state.status.value == "inactive"
        state = adapt_asset(_asset_payload(status=_EnumLike("inactive")))
        assert state.status is AssetStatus.INACTIVE
        assert state.status.value == "inactive"

    def test_dotted_asset_status_string_is_rejected(self) -> None:
        with pytest.raises(InvalidBrokerStateError, match="unknown asset status"):
            adapt_asset(_asset_payload(status="AssetStatus.ACTIVE"))

    def test_foo_active_is_rejected(self) -> None:
        with pytest.raises(InvalidBrokerStateError, match="unknown asset status"):
            adapt_asset(_asset_payload(status="foo.active"))

    def test_not_active_is_rejected(self) -> None:
        with pytest.raises(InvalidBrokerStateError, match="unknown asset status"):
            adapt_asset(_asset_payload(status="not_active"))

    def test_unknown_enum_value_is_rejected(self) -> None:
        with pytest.raises(InvalidBrokerStateError, match="unknown asset status"):
            adapt_asset(_asset_payload(status="delisted"))

    def test_none_status_is_rejected(self) -> None:
        payload = _asset_payload()
        payload["status"] = None
        with pytest.raises(InvalidBrokerStateError, match="missing broker field 'status'"):
            adapt_asset(payload)

    def test_arbitrary_object_is_rejected(self) -> None:
        with pytest.raises(InvalidBrokerStateError, match="unknown asset status"):
            adapt_asset(_asset_payload(status=object()))
        with pytest.raises(InvalidBrokerStateError, match="unknown asset status"):
            adapt_asset(_asset_payload(status=_ContainsActive()))

    def test_missing_status_is_rejected(self) -> None:
        with pytest.raises(InvalidBrokerStateError, match="missing broker field 'status'"):
            adapt_asset(_asset_payload(include_status=False))

    def test_tradable_non_bool_is_rejected(self) -> None:
        with pytest.raises(InvalidBrokerStateError, match="tradable/fractionable must be bool"):
            adapt_asset(_asset_payload(tradable="true"))

    def test_fractionable_non_bool_is_rejected(self) -> None:
        with pytest.raises(InvalidBrokerStateError, match="tradable/fractionable must be bool"):
            adapt_asset(_asset_payload(fractionable=1))
