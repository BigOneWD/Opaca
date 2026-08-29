"""P0-C: stale state / versioning."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from opaca.domain.models import AuthorityResult, SettlementEvent, Side
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.store import SQLiteStore
from opaca.persistence.types import ReconciliationStatus
from opaca.reconciliation.service import reconcile

from probe_support import (
    DEFAULT_NOW,
    DEFAULT_PRICES,
    make_order,
    make_proposal,
    paper_gateway,
    position_payload,
    reconciled_store,
    temp_store,
)

SGOV = DEFAULT_PRICES["SGOV"]


def _sell(store, pid, version, qty="10"):
    proposal = make_proposal(pid, [make_order(pid, 0, "SGOV", Side.SELL, qty, SGOV)])
    return evaluate_and_reserve(
        store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
        expected_snapshot_version=version,
    )


def test_c01_position_change_advances_version_and_blocks_old_decision(tmp_path):
    store, v1 = reconciled_store(tmp_path, qty="100")
    reconcile(store, paper_gateway(positions=(position_payload(qty="40"),)), now=DEFAULT_NOW)
    out = _sell(store, "stale-pos", v1)
    assert out.blocked and not out.reserved
    assert store.get_proposal("stale-pos") is None
    store.close()


def test_c02_cash_change_advances_version_and_blocks_old_decision(tmp_path):
    store, v1 = reconciled_store(tmp_path, qty="100")
    reconcile(
        store,
        paper_gateway(cash="80000", positions=(position_payload(qty="100"),)),
        now=DEFAULT_NOW,
    )
    out = _sell(store, "stale-cash", v1)
    assert out.blocked and not out.reserved
    store.close()


def test_c03_unresolved_broker_order_appearing_blocks_old_decision(tmp_path):
    from tests.state_helpers import order_payload

    store, v1 = reconciled_store(tmp_path, qty="100")
    reconcile(
        store,
        paper_gateway(
            positions=(position_payload(qty="100"),),
            open_orders=(order_payload("broker-unknown-1", status="new", qty="50"),),
        ),
        now=DEFAULT_NOW,
    )
    out = _sell(store, "stale-order", v1)
    assert out.blocked and not out.reserved
    store.close()


def test_c04_settlement_change_is_visible_to_the_reserving_transaction(tmp_path):
    """A settlement event added after evaluation must be seen by the reserve txn."""
    store, v1 = reconciled_store(tmp_path, qty=None, cash="100000")
    store.insert_settlement_event(
        SettlementEvent(
            event_id="unsettled-1",
            symbol="SGOV",
            trade_date=DEFAULT_NOW.date(),
            settlement_date=DEFAULT_NOW.date() + timedelta(days=1),
            amount=Decimal("95000"),
        ),
        now=DEFAULT_NOW,
    )
    proposal = make_proposal("buy-after", [make_order("buy-after", 0, "SGOV", Side.BUY, "99", SGOV)])
    out = evaluate_and_reserve(
        store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
        expected_snapshot_version=v1,
    )
    assert out.authority_result is not AuthorityResult.AUTO
    assert out.reserved is False
    store.close()


def test_c05_policy_change_is_read_inside_the_reserving_transaction(tmp_path):
    store, v1 = reconciled_store(tmp_path, qty="100")
    store._conn.execute(
        "UPDATE policies SET value = '1.00' WHERE name = 'per_order_autonomous_notional_max'"
    )
    out = _sell(store, "policy-change", v1)
    assert out.authority_result is AuthorityResult.APPROVAL_REQUIRED
    assert out.reserved is False
    store.close()


def test_c05b_corrupt_policy_row_fails_closed_but_escapes_as_raw_moneyerror(tmp_path):
    """P2: an out-of-range policy row escapes evaluate_and_reserve as MoneyError,
    not as a blocked OrchestrationResult. Fail-closed, but a different shape."""
    from opaca.domain.money import MoneyError

    store, v1 = reconciled_store(tmp_path, qty="100")
    store._conn.execute(
        "UPDATE policies SET value = '0' WHERE name = 'per_order_autonomous_notional_max'"
    )
    with pytest.raises(MoneyError):
        _sell(store, "corrupt-policy", v1)
    assert store.get_proposal("corrupt-policy") is None
    assert store.count_reservations("corrupt-policy") == 0
    store.close()


def test_c06_kill_switch_is_read_inside_the_reserving_transaction(tmp_path):
    store, v1 = reconciled_store(tmp_path, qty="100")
    store.set_kill_switch(True, now=DEFAULT_NOW)
    out = _sell(store, "killed", v1)
    assert out.authority_result is AuthorityResult.REJECT
    assert out.reserved is False
    store.close()


def test_c07_non_reconciled_latest_snapshot_blocks_even_with_matching_version(tmp_path):
    from tests.state_helpers import order_payload

    store, _ = reconciled_store(tmp_path, qty="100")
    recon = reconcile(
        store,
        paper_gateway(
            positions=(position_payload(qty="100"),),
            open_orders=(order_payload("ghost", status="new", qty="50"),),
        ),
        now=DEFAULT_NOW,
    )
    assert recon.status is not ReconciliationStatus.RECONCILED
    assert recon.snapshot is not None
    out = _sell(store, "drifted", recon.snapshot.version)
    assert out.blocked and not out.reserved
    store.close()


# ---------------------------------------------------------------- FINDINGS


def test_c08_FINDING_no_snapshot_age_bound(tmp_path):
    """A RECONCILED snapshot never expires: 30 days later it is still executable."""
    store, v1 = reconciled_store(tmp_path, qty="100")
    much_later = DEFAULT_NOW + timedelta(days=30)
    proposal = make_proposal("ancient", [make_order("ancient", 0, "SGOV", Side.SELL, "10", SGOV)])
    out = evaluate_and_reserve(
        store, proposal, now=much_later, prices=DEFAULT_PRICES,
        expected_snapshot_version=v1,
    )
    snap = store.latest_snapshot()
    age = much_later - snap.captured_at
    assert age > timedelta(days=29)
    assert out.is_auto, "probe assumption"
    pytest.fail(
        f"FINDING P0-C-1: reserved AUTO against a snapshot {age} old; "
        "no maximum snapshot age is enforced anywhere"
    )


def test_c09_FINDING_expected_snapshot_version_is_optional(tmp_path):
    """Omitting expected_snapshot_version disables the staleness gate entirely."""
    store, v1 = reconciled_store(tmp_path, qty="100")
    reconcile(store, paper_gateway(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
    v2 = store.latest_snapshot().version
    assert v2 != v1
    proposal = make_proposal("nover", [make_order("nover", 0, "SGOV", Side.SELL, "10", SGOV)])
    out = evaluate_and_reserve(store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES)
    assert out.is_auto, "probe assumption"
    pytest.fail(
        "FINDING P0-C-2: evaluate_and_reserve() defaults expected_snapshot_version=None, "
        "so a caller that evaluated against snapshot N silently reserves against N+k"
    )
