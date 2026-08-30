"""Optional live PAPER read-only smoke. Never mutates broker state."""

from __future__ import annotations

import os

import pytest
from opaca.broker.gateway import (
    ASSET_UNIVERSE,
    assert_read_only_gateway,
    gateway_methods_are_read_only,
)
from opaca.broker.mutation import FORBIDDEN_BROKER_MUTATIONS
from opaca.broker.paper import ENV_KEY_ID, ENV_SECRET


@pytest.mark.live_paper
def test_live_paper_read_only_smoke() -> None:
    if not os.environ.get(ENV_KEY_ID, "").strip() or not os.environ.get(ENV_SECRET, "").strip():
        pytest.fail("live paper smoke requested but paper credentials are not present")
    from opaca.broker.alpaca import AlpacaPaperGateway, open_paper_gateway_from_env

    assert gateway_methods_are_read_only(AlpacaPaperGateway)
    gateway = open_paper_gateway_from_env()
    assert_read_only_gateway(gateway)
    assert getattr(gateway, "_client", None) is None
    for name in FORBIDDEN_BROKER_MUTATIONS:
        assert not callable(getattr(gateway, name, None))
    account = gateway.get_account()
    assert "cash" in account
    assert "id" in account or "cash" in account
    positions = gateway.get_positions()
    assert isinstance(positions, tuple | list)
    for symbol in ASSET_UNIVERSE:
        asset = gateway.get_asset(symbol)
        assert str(asset.get("symbol", symbol)) == symbol
    orders = gateway.get_open_orders()
    assert isinstance(orders, tuple | list)
    clock = gateway.get_clock()
    assert clock is not None
    _ = orders
    _ = positions
    _ = clock
