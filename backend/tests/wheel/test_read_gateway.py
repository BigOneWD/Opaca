"""RED-phase contracts for the authoritative option read-only gateway."""

from __future__ import annotations

from opaca.wheel.read_gateway import AlpacaOptionReadGateway, OptionReadGateway


class TradingReadClient:
    def __init__(self) -> None:
        self.option_contract_request: object | None = None
        self.order_filter: object | None = None

    def get_account(self) -> dict[str, object]:
        return {"cash": "99899.58", "status": "ACTIVE"}

    def get_option_contracts(self, request: object) -> list[dict[str, object]]:
        self.option_contract_request = request
        return [{"symbol": "SPY260903P00746000"}]

    def get_option_contract(self, symbol_or_id: str) -> dict[str, object]:
        return {"symbol": symbol_or_id, "type": "put"}

    def get_all_positions(self) -> list[dict[str, object]]:
        return [
            {"asset_class": "us_option", "symbol": "SPY260903P00746000"},
            {"asset_class": "us_equity", "symbol": "SPY"},
        ]

    def get_orders(self, filter: object = None) -> list[dict[str, object]]:
        self.order_filter = filter
        return [{"client_order_id": "wheel-order-1", "symbol": "SPY260903P00746000"}]

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, object]:
        return {"client_order_id": client_order_id}

    def get_clock(self) -> dict[str, object]:
        return {"is_open": True}


class OptionDataReadClient:
    def __init__(self) -> None:
        self.quote_request: object | None = None

    def get_option_latest_quote(self, request: object) -> dict[str, dict[str, object]]:
        self.quote_request = request
        return {"SPY260903P00746000": {"bid_price": "0.07", "ask_price": "0.08"}}


def test_protocol_exposes_only_v1_reads_and_no_mutations() -> None:
    names = {name for name in dir(OptionReadGateway) if not name.startswith("_")}
    required = {
        "get_account",
        "get_option_contracts",
        "get_option_contract",
        "get_option_quote",
        "get_option_positions",
        "get_equity_positions",
        "get_open_orders",
        "get_order_by_client_id",
        "get_clock",
    }
    forbidden = {
        "submit_order",
        "place_option_order",
        "cancel_order",
        "cancel_all_orders",
        "replace_order",
        "exercise_option",
        "close_position",
        "update_account",
    }

    assert required <= names
    assert names & forbidden == set()


def test_concrete_gateway_forwards_only_injected_read_clients() -> None:
    trading = TradingReadClient()
    option_data = OptionDataReadClient()
    gateway = AlpacaOptionReadGateway(
        trading_client=trading,
        option_data_client=option_data,
    )

    assert gateway.get_account()["status"] == "ACTIVE"
    assert gateway.get_option_contract("SPY260903P00746000")["type"] == "put"
    assert gateway.get_option_contracts("SPY") == [{"symbol": "SPY260903P00746000"}]
    assert gateway.get_option_quote("SPY260903P00746000")["bid_price"] == "0.07"
    assert gateway.get_option_positions() == [
        {"asset_class": "us_option", "symbol": "SPY260903P00746000"}
    ]
    assert gateway.get_equity_positions() == [{"asset_class": "us_equity", "symbol": "SPY"}]
    assert gateway.get_open_orders() == [
        {"client_order_id": "wheel-order-1", "symbol": "SPY260903P00746000"}
    ]
    assert gateway.get_order_by_client_id("wheel-order-1") == {
        "client_order_id": "wheel-order-1"
    }
    assert gateway.get_clock() == {"is_open": True}
    assert isinstance(trading.option_contract_request, object)
    assert isinstance(trading.order_filter, object)
    assert isinstance(option_data.quote_request, object)


def test_concrete_gateway_does_not_expose_mutation_capabilities() -> None:
    gateway = AlpacaOptionReadGateway(
        trading_client=TradingReadClient(),
        option_data_client=OptionDataReadClient(),
    )
    forbidden = (
        "submit_order",
        "place_option_order",
        "cancel_order",
        "cancel_all_orders",
        "replace_order",
        "exercise_option",
        "close_position",
        "update_account",
    )

    assert all(not callable(getattr(gateway, name, None)) for name in forbidden)


def test_gateway_reads_relevant_open_orders_and_order_lookup_without_network() -> None:
    trading = TradingReadClient()
    gateway = AlpacaOptionReadGateway(
        trading_client=trading,
        option_data_client=OptionDataReadClient(),
    )

    gateway.get_open_orders()
    gateway.get_order_by_client_id("wheel-order-2")

    assert trading.order_filter is not None
