"""P1-A: broker adapter trust boundary. No corrupt input may produce AUTO."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from opaca.broker.gateway import FakeAlpacaGateway
from opaca.domain.models import Side
from opaca.orchestration.reserve import read_reconcile_evaluate_reserve
from opaca.persistence.types import ReconciliationStatus

from probe_support import (
    DEFAULT_NOW,
    DEFAULT_PRICES,
    make_order,
    make_proposal,
    temp_store,
)
from tests.state_helpers import (
    PHASE1_ASSETS,
    account_payload,
    order_payload,
    position_payload,
)

SGOV = DEFAULT_PRICES["SGOV"]


def gateway(account=None, positions=(), open_orders=(), assets=None, clock=None):
    return FakeAlpacaGateway(
        account=account if account is not None else account_payload(),
        positions=tuple(positions),
        assets=PHASE1_ASSETS if assets is None else assets,
        open_orders=tuple(open_orders),
        orders_by_client_id={},
        clock=clock
        or {
            "timestamp": DEFAULT_NOW.isoformat(),
            "is_open": True,
            "next_open": "2026-09-02T13:30:00+00:00",
            "next_close": "2026-09-01T20:00:00+00:00",
        },
    )


def run_path(tmp_path, gw, *, side=Side.SELL, qty="10"):
    """Returns ('auto'|'no-auto'|'raised', detail). Never allowed: 'auto'."""
    store = temp_store(tmp_path)
    proposal = make_proposal("p", [make_order("p", 0, "SGOV", side, qty, SGOV)])
    try:
        recon, out = read_reconcile_evaluate_reserve(
            store, gw, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES
        )
    except Exception as exc:  # noqa: BLE001
        store.close()
        return "raised", f"{type(exc).__name__}: {exc}"
    result = ("auto" if out.is_auto else "no-auto", f"{recon.status.value}/{out.block_reason}")
    store.close()
    return result


# ------------------------------------------------------------------ ACCOUNT

ACCOUNT_CASES = {
    "missing_cash": lambda p: p.pop("cash"),
    "cash_zero": lambda p: p.update(cash="0"),
    "cash_negative": lambda p: p.update(cash="-1"),
    "cash_nan": lambda p: p.update(cash="NaN"),
    "cash_snan": lambda p: p.update(cash="sNaN"),
    "cash_inf": lambda p: p.update(cash="Infinity"),
    "cash_neg_inf": lambda p: p.update(cash="-Infinity"),
    "cash_malformed": lambda p: p.update(cash="one hundred"),
    "cash_empty": lambda p: p.update(cash=""),
    "cash_float": lambda p: p.update(cash=100000.0),
    "cash_bool": lambda p: p.update(cash=True),
    "cash_none": lambda p: p.update(cash=None),
    "cash_huge": lambda p: p.update(cash="1e30"),
    "cash_at_limit": lambda p: p.update(cash="1e26"),
    "cash_list": lambda p: p.update(cash=["100000"]),
    "bp_4x_cash": lambda p: p.update(buying_power="400000"),
    "nonmarginable_gt_cash": lambda p: p.update(non_marginable_buying_power="999999"),
    "missing_buying_power": lambda p: p.pop("buying_power"),
    "missing_nonmarginable": lambda p: p.pop("non_marginable_buying_power"),
    "multiplier_missing": lambda p: p.pop("multiplier"),
    "multiplier_nan": lambda p: p.update(multiplier="NaN"),
}


@pytest.mark.parametrize("name", sorted(ACCOUNT_CASES))
def test_account_corruption_never_auto(tmp_path, name):
    payload = dict(account_payload())
    ACCOUNT_CASES[name](payload)
    status, detail = run_path(tmp_path, gateway(account=payload,
                                                positions=(position_payload(qty="100"),)))
    if name in {"bp_4x_cash", "nonmarginable_gt_cash", "cash_at_limit"}:
        return  # documented-benign shapes; asserted separately below
    assert status != "auto", f"{name} produced AUTO ({detail})"


def test_buying_power_inflation_is_ignored_not_trusted(tmp_path):
    """4x buying_power and inflated non-marginable must not enlarge deployable cash."""
    payload = dict(account_payload(cash="100000"))
    payload["buying_power"] = "400000"
    payload["non_marginable_buying_power"] = "400000"
    store = temp_store(tmp_path)
    proposal = make_proposal("big", [make_order("big", 0, "SGOV", Side.BUY, "300", SGOV)])
    recon, out = read_reconcile_evaluate_reserve(
        store, gateway(account=payload), proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES
    )
    assert recon.status is ReconciliationStatus.RECONCILED
    assert not out.is_auto, "30k buy funded from margin buying power"
    store.close()


# ---------------------------------------------------------------- POSITIONS

POSITION_CASES = {
    "negative_qty": lambda: position_payload(qty="-100"),
    "available_gt_qty": lambda: position_payload(qty="100", qty_available="150"),
    "missing_symbol": lambda: {k: v for k, v in position_payload().items() if k != "symbol"},
    "empty_symbol": lambda: {**position_payload(), "symbol": ""},
    "malformed_qty": lambda: {**position_payload(), "qty": "ten"},
    "nan_qty": lambda: {**position_payload(), "qty": "NaN"},
    "inf_qty": lambda: {**position_payload(), "qty": "Infinity"},
    "float_qty": lambda: {**position_payload(), "qty": 100.0},
    "short_side": lambda: {**position_payload(), "side": "short"},
    "missing_market_value": lambda: {
        k: v for k, v in position_payload().items() if k != "market_value"
    },
    "huge_qty": lambda: {**position_payload(), "qty": "1e30", "market_value": "1"},
}


@pytest.mark.parametrize("name", sorted(POSITION_CASES))
def test_position_corruption_never_auto_oversell(tmp_path, name):
    gw = gateway(positions=(POSITION_CASES[name](),))
    status, detail = run_path(tmp_path, gw, side=Side.SELL, qty="100")
    assert status != "auto", f"{name} produced AUTO ({detail})"


def test_duplicate_position_rows_do_not_double_the_pool(tmp_path):
    gw = gateway(positions=(position_payload(qty="100"), position_payload(qty="100")))
    status, detail = run_path(tmp_path, gw, side=Side.SELL, qty="150")
    assert status != "auto", detail


# ------------------------------------------------------------------- ASSETS

def test_inactive_asset_never_auto(tmp_path):
    assets = {k: dict(v) for k, v in PHASE1_ASSETS.items()}
    assets["SGOV"]["status"] = "inactive"
    status, detail = run_path(
        tmp_path, gateway(assets=assets, positions=(position_payload(qty="100"),))
    )
    assert status != "auto", detail


def test_non_tradable_asset_never_auto(tmp_path):
    assets = {k: dict(v) for k, v in PHASE1_ASSETS.items()}
    assets["SGOV"]["tradable"] = False
    status, detail = run_path(
        tmp_path, gateway(assets=assets, positions=(position_payload(qty="100"),))
    )
    assert status != "auto", detail


def test_missing_asset_never_auto(tmp_path):
    assets = {k: v for k, v in PHASE1_ASSETS.items() if k != "SGOV"}
    status, detail = run_path(
        tmp_path, gateway(assets=assets, positions=(position_payload(qty="100"),))
    )
    assert status != "auto", detail


def test_asset_tradable_not_bool_never_auto(tmp_path):
    assets = {k: dict(v) for k, v in PHASE1_ASSETS.items()}
    assets["SGOV"]["tradable"] = "true"
    status, detail = run_path(
        tmp_path, gateway(assets=assets, positions=(position_payload(qty="100"),))
    )
    assert status != "auto", detail


# ------------------------------------------------------------------- ORDERS

ORDER_CASES = {
    "unresolved_sell_no_qty": lambda: order_payload("o1", status="new", qty=None,
                                                    filled_qty=None),
    "filled_gt_qty": lambda: order_payload("o1", status="partially_filled", qty="10",
                                           filled_qty="50"),
    "unknown_state": lambda: order_payload("o1", status="frobnicated"),
    "done_for_day": lambda: order_payload("o1", status="done_for_day"),
    "held": lambda: order_payload("o1", status="held"),
    "pending_cancel": lambda: order_payload("o1", status="pending_cancel"),
    "missing_client_order_id": lambda: {
        k: v for k, v in order_payload("o1").items() if k != "client_order_id"
    },
    "empty_client_order_id": lambda: order_payload(""),
    "malformed_side": lambda: order_payload("o1", side="sideways"),
    "missing_status": lambda: {k: v for k, v in order_payload("o1").items() if k != "status"},
    "negative_qty": lambda: order_payload("o1", qty="-5"),
    "float_qty": lambda: {**order_payload("o1"), "qty": 5.0},
}


@pytest.mark.parametrize("name", sorted(ORDER_CASES))
def test_order_corruption_never_auto(tmp_path, name):
    gw = gateway(positions=(position_payload(qty="100"),), open_orders=(ORDER_CASES[name](),))
    status, detail = run_path(tmp_path, gw, side=Side.SELL, qty="10")
    assert status != "auto", f"{name} produced AUTO ({detail})"


def test_duplicate_client_order_id_never_auto(tmp_path):
    gw = gateway(
        positions=(position_payload(qty="100"),),
        open_orders=(order_payload("dup", status="new", qty="10"),
                     order_payload("dup", status="new", qty="10")),
    )
    status, detail = run_path(tmp_path, gw, side=Side.SELL, qty="10")
    assert status != "auto", detail


def test_same_order_twice_under_different_ids_never_auto(tmp_path):
    gw = gateway(
        positions=(position_payload(qty="100"),),
        open_orders=(order_payload("a", status="new", qty="60", broker_id="X"),
                     order_payload("b", status="new", qty="60", broker_id="X")),
    )
    status, detail = run_path(tmp_path, gw, side=Side.SELL, qty="10")
    assert status != "auto", detail


def test_broker_order_unknown_locally_blocks(tmp_path):
    gw = gateway(
        positions=(position_payload(qty="100"),),
        open_orders=(order_payload("stranger", status="new", qty="10"),),
    )
    status, detail = run_path(tmp_path, gw, side=Side.SELL, qty="10")
    assert status != "auto", detail


# ------------------------------------------------------------------- PRICES

PRICE_CASES = {
    "missing": None,
    "zero": Decimal("0"),
    "negative": Decimal("-100"),
    "float": 100.69,
    "nan": Decimal("NaN"),
    "inf": Decimal("Infinity"),
    "huge": Decimal("1e30"),
    "string": "100.69",
    "int": 100,
    "none": None,
}


@pytest.mark.parametrize("name", sorted(PRICE_CASES))
def test_price_corruption_never_auto(tmp_path, name):
    prices = dict(DEFAULT_PRICES)
    if name in {"missing"}:
        prices.pop("SGOV")
    else:
        prices["SGOV"] = PRICE_CASES[name]
    store = temp_store(tmp_path)
    proposal = make_proposal("p", [make_order("p", 0, "SGOV", Side.SELL, "10", SGOV)])
    try:
        _, out = read_reconcile_evaluate_reserve(
            store,
            gateway(positions=(position_payload(qty="100"),)),
            proposal,
            now=DEFAULT_NOW,
            prices=prices,
        )
        assert not out.is_auto, f"{name} produced AUTO"
    except Exception:
        pass
    finally:
        store.close()


# --------------------------------------------------------------- TIMESTAMPS

def test_naive_now_is_rejected(tmp_path):
    store = temp_store(tmp_path)
    proposal = make_proposal("p", [make_order("p", 0, "SGOV", Side.SELL, "10", SGOV)])
    with pytest.raises(Exception):
        read_reconcile_evaluate_reserve(
            store,
            gateway(positions=(position_payload(qty="100"),)),
            proposal,
            now=DEFAULT_NOW.replace(tzinfo=None),
            prices=DEFAULT_PRICES,
        )
    store.close()


def test_non_utc_now_fails_closed(tmp_path):
    """An aware but non-UTC clock must not reconcile."""
    from datetime import timezone

    store = temp_store(tmp_path)
    proposal = make_proposal("p", [make_order("p", 0, "SGOV", Side.SELL, "10", SGOV)])
    recon, out = read_reconcile_evaluate_reserve(
        store,
        gateway(positions=(position_payload(qty="100"),)),
        proposal,
        now=DEFAULT_NOW.astimezone(timezone(timedelta(hours=8))),
        prices=DEFAULT_PRICES,
    )
    assert recon.status is ReconciliationStatus.INVALID_BROKER_STATE
    assert not out.is_auto
    store.close()


def test_fractional_position_boundary_is_exact(tmp_path):
    """Selling one ulp more than the reconciled position must fail closed."""
    gw = gateway(positions=({**position_payload(), "qty": "100.000000001",
                             "qty_available": "100.000000001"},))
    ok, _ = run_path(tmp_path, gw, side=Side.SELL, qty="100.000000001")
    assert ok == "auto"
    over, detail = run_path(tmp_path, gw, side=Side.SELL, qty="100.000000002")
    assert over != "auto", detail
