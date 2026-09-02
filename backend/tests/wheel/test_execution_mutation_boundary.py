"""RED-phase contracts for the concrete paper-only execution boundary."""

from __future__ import annotations

from decimal import Decimal

import pytest
from opaca.wheel.execution import (
    OptionOrderRequest,
    PaperAlpacaOptionExecutionGateway,
    PaperExecutionConfigurationError,
)


class FakeTradingClient:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def submit_order(self, request: object) -> object:
        self.calls.append(request)
        return {"status": "accepted"}


def request() -> OptionOrderRequest:
    return OptionOrderRequest(
        symbol="SPY260904P00100000",
        contracts=1,
        side="SELL",
        order_type="LIMIT",
        time_in_force="DAY",
        limit_premium=Decimal("1.00"),
        client_order_id="wheel-abc123",
    )


@pytest.mark.parametrize(
    "paper,paper_endpoint_verified",
    [(False, True), (True, False)],
)
def test_concrete_gateway_rejects_live_or_unverified_configuration(
    paper: bool,
    paper_endpoint_verified: bool,
) -> None:
    with pytest.raises(PaperExecutionConfigurationError):
        PaperAlpacaOptionExecutionGateway(
            trading_client=FakeTradingClient(),
            paper=paper,
            paper_endpoint_verified=paper_endpoint_verified,
        )


def test_concrete_gateway_is_paper_only_and_submits_one_option_request() -> None:
    client = FakeTradingClient()
    gateway = PaperAlpacaOptionExecutionGateway(
        trading_client=client,
        paper=True,
        paper_endpoint_verified=True,
    )

    result = gateway.submit_order(request())

    assert result == {"status": "accepted"}
    assert len(client.calls) == 1

