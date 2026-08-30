"""Narrow remediation retest of the seven findings closed at d85a2e6.

Every test here asserts the FIXED behaviour. Nothing in this file is a
characterisation marker; a failure means the remediation is incomplete.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest
from opaca.broker.errors import InvalidBrokerStateError
from opaca.broker.gateway import FakeAlpacaGateway, assert_read_only_gateway
from opaca.domain.models import AuthorityResult, OrderState, Side
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.codec import dump_datetime
from opaca.persistence.store import SQLiteStore
from opaca.persistence.types import (
    AuditEventType,
    ReconciliationStatus,
    UnknownOrderRecord,
)
from opaca.reconciliation.service import reconcile
from tests.state_helpers import PHASE1_ASSETS, account_payload, order_payload

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
MAX_AGE = timedelta(seconds=60)


def gw(**kw):
    base = dict(
        account=account_payload(),
        positions=(),
        assets=PHASE1_ASSETS,
        open_orders=(),
        orders_by_client_id={},
        clock={"timestamp": DEFAULT_NOW.isoformat(), "is_open": True},
    )
    base.update(kw)
    return FakeAlpacaGateway(**base)


def _sell(store, pid, version, qty="10", now=DEFAULT_NOW):
    p = make_proposal(pid, [make_order(pid, 0, "SGOV", Side.SELL, qty, SGOV)])
    return evaluate_and_reserve(
        store, p, now=now, prices=DEFAULT_PRICES, expected_snapshot_version=version
    )


def _auto(store, version, pid="live", qty="10", now=DEFAULT_NOW):
    p = make_proposal(pid, [make_order(pid, 0, "SGOV", Side.SELL, qty, SGOV)])
    out = evaluate_and_reserve(
        store, p, now=now, prices=DEFAULT_PRICES, expected_snapshot_version=version
    )
    assert out.is_auto, (out.authority_result, out.block_reason)
    return p, out


# =====================================================================  P0-1


class TestP01ReplaySafety:
    def test_replay_under_kill_switch_is_not_auto(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto(store, v)
        store.set_kill_switch(True, now=DEFAULT_NOW)
        replay = evaluate_and_reserve(
            store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
            expected_snapshot_version=v,
        )
        assert replay.is_auto is False
        assert replay.blocked is True
        assert replay.reserved is False
        assert replay.block_reason == "kill switch active"
        assert replay.idempotent_replay is True
        assert AuditEventType.RESERVATION_DENIED in [
            e.event_type for e in store.list_audit(proposal_id="live")
        ]
        store.close()

    def test_replay_under_drift_is_not_auto(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto(store, v)
        recon = reconcile(
            store,
            gw(positions=(position_payload(qty="100"),),
               open_orders=(order_payload("ghost", status="new", qty="5"),)),
            now=DEFAULT_NOW,
        )
        assert recon.status is ReconciliationStatus.DRIFT_DETECTED
        replay = evaluate_and_reserve(
            store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
            expected_snapshot_version=recon.snapshot.version,
        )
        assert replay.is_auto is False and replay.blocked is True
        assert "DRIFT_DETECTED" in replay.block_reason
        store.close()

    def test_replay_under_unknown_requires_review_is_not_auto(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto(store, v)
        recon = reconcile(
            store,
            gw(positions=(position_payload(qty="100"),),
               open_orders=(order_payload("weird", status="frobnicated"),)),
            now=DEFAULT_NOW,
        )
        assert recon.status is ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW
        replay = evaluate_and_reserve(
            store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
            expected_snapshot_version=recon.snapshot.version,
        )
        assert replay.is_auto is False and replay.blocked is True
        assert "UNKNOWN_REQUIRES_REVIEW" in replay.block_reason
        store.close()

    def test_replay_against_stale_snapshot_age_is_not_auto(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto(store, v)
        later = DEFAULT_NOW + timedelta(days=30)
        replay = evaluate_and_reserve(
            store, proposal, now=later, prices=DEFAULT_PRICES, expected_snapshot_version=v
        )
        assert replay.is_auto is False and replay.blocked is True
        assert replay.block_reason == "stale snapshot"
        stale = store.list_audit(event_type=AuditEventType.STALE_SNAPSHOT)
        assert stale, "a stale replay must be audited"
        store.close()

    def test_replay_with_wrong_version_is_not_auto(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto(store, v)
        reconcile(store, gw(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
        v2 = store.latest_snapshot().version
        assert v2 != v
        replay = evaluate_and_reserve(
            store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES, expected_snapshot_version=v
        )
        assert replay.is_auto is False and replay.blocked is True
        assert replay.block_reason == "stale snapshot"
        store.close()

    def test_replay_with_missing_expected_version_is_not_auto(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto(store, v)
        replay = evaluate_and_reserve(
            store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES
        )
        assert replay.is_auto is False and replay.blocked is True
        assert replay.block_reason == "expected_snapshot_version is required"
        store.close()

    def test_replay_with_changed_payload_is_blocked_and_adds_nothing(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        _auto(store, v, pid="mut", qty="10")
        mutated = make_proposal("mut", [make_order("mut", 0, "SGOV", Side.SELL, "90", SGOV)])
        out = evaluate_and_reserve(
            store, mutated, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
            expected_snapshot_version=v,
        )
        assert out.blocked is True and out.reserved is False
        assert out.block_reason == "proposal_id reused with a different payload"
        assert out.idempotent_replay is False
        assert sum(
            (r.quantity for r in store.active_reservations()
             if r.quantity is not None and r.kind.value == "SELL_QUANTITY"),
            Decimal("0"),
        ) == Decimal("10")
        store.close()

    def test_clean_replay_still_reports_auto_and_adds_no_capacity(self, tmp_path):
        """The fix must not break idempotency: a replay under current, matching,
        fresh, reconciled state is still AUTO and still capacity-neutral."""
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, first = _auto(store, v)
        before = (len(store.load_autonomous_history()), store.count_reservations("live"))
        for _ in range(3):
            replay = evaluate_and_reserve(
                store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                expected_snapshot_version=v,
            )
            assert replay.is_auto is True
            assert replay.idempotent_replay is True
        assert (len(store.load_autonomous_history()), store.count_reservations("live")) == before
        store.close()

    def test_replay_of_a_rejected_proposal_never_becomes_auto(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        out = _sell(store, "big", v, qty="500")
        assert out.authority_result is AuthorityResult.REJECT
        replay = _sell(store, "big", v, qty="500")
        assert replay.is_auto is False
        assert replay.reserved is False
        store.close()


# =====================================================================  P0-2


class TestP02SnapshotFreshness:
    def test_expected_version_is_mandatory_on_a_fresh_proposal(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        p = make_proposal("nov", [make_order("nov", 0, "SGOV", Side.SELL, "10", SGOV)])
        out = evaluate_and_reserve(store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES)
        assert out.blocked is True and out.reserved is False
        assert out.block_reason == "expected_snapshot_version is required"
        assert store.get_proposal("nov") is None
        assert store.count_reservations("nov") == 0
        store.close()

    def test_thirty_day_old_snapshot_is_refused(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        out = _sell(store, "old", v, now=DEFAULT_NOW + timedelta(days=30))
        assert out.blocked is True and out.block_reason == "stale snapshot"
        assert store.get_proposal("old") is None
        store.close()

    def test_exact_max_age_boundary_is_accepted(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        out = _sell(store, "edge", v, now=DEFAULT_NOW + MAX_AGE)
        assert out.is_auto is True, (out.block_reason, out.authority_result)
        store.close()

    def test_one_microsecond_beyond_max_age_is_refused(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        out = _sell(
            store, "edge2", v, now=DEFAULT_NOW + MAX_AGE + timedelta(microseconds=1)
        )
        assert out.blocked is True and out.block_reason == "stale snapshot"
        assert store.get_proposal("edge2") is None
        store.close()

    def test_max_age_is_policy_driven_not_hardcoded(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        assert store.policy_value("max_snapshot_age_seconds") == "60"
        store._conn.execute(
            "UPDATE policies SET value = '5' WHERE name = 'max_snapshot_age_seconds'"
        )
        assert _sell(store, "a", v, now=DEFAULT_NOW + timedelta(seconds=5)).is_auto
        assert _sell(store, "b", v, now=DEFAULT_NOW + timedelta(seconds=6)).blocked
        store.close()

    def test_future_snapshot_is_refused(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        out = _sell(store, "fut", v, now=DEFAULT_NOW - timedelta(seconds=1))
        assert out.blocked is True
        assert out.block_reason == "snapshot captured_at is in the future"
        store.close()

    def test_future_snapshot_is_refused_on_replay_too(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto(store, v)
        replay = evaluate_and_reserve(
            store, proposal, now=DEFAULT_NOW - timedelta(hours=1), prices=DEFAULT_PRICES,
            expected_snapshot_version=v,
        )
        assert replay.is_auto is False and replay.blocked is True
        store.close()

    def test_naive_now_fails_closed(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        p = make_proposal("naive", [make_order("naive", 0, "SGOV", Side.SELL, "10", SGOV)])
        outcome = None
        raised = None
        try:
            outcome = evaluate_and_reserve(
                store, p, now=DEFAULT_NOW.replace(tzinfo=None), prices=DEFAULT_PRICES,
                expected_snapshot_version=v,
            )
        except Exception as exc:  # noqa: BLE001
            raised = exc
        assert raised is not None or (outcome is not None and not outcome.is_auto)
        assert store.get_proposal("naive") is None
        assert store.count_reservations("naive") == 0
        store.close()

    def test_non_utc_aware_now_fails_closed(self, tmp_path):
        """`now` must be UTC. A non-UTC aware clock never produces a reservation.

        The freshness gate itself compares instants, so a +08:00 clock inside the
        age window passes the gate; ExecutionContext then rejects the offset and
        the call fails closed without persisting anything.
        """
        from datetime import timezone

        store, v = reconciled_store(tmp_path, qty="100")
        sgt = timezone(timedelta(hours=8))
        p = make_proposal("sgt", [make_order("sgt", 0, "SGOV", Side.SELL, "10", SGOV)])
        outcome = None
        raised = None
        try:
            outcome = evaluate_and_reserve(
                store, p, now=(DEFAULT_NOW + timedelta(seconds=1)).astimezone(sgt),
                prices=DEFAULT_PRICES, expected_snapshot_version=v,
            )
        except Exception as exc:  # noqa: BLE001
            raised = exc
        assert raised is not None or (outcome is not None and not outcome.is_auto)
        assert store.get_proposal("sgt") is None
        assert store.count_reservations("sgt") == 0
        assert store.active_reservations() == ()
        store.close()

    def test_non_utc_aware_now_beyond_max_age_is_refused_by_the_gate(self, tmp_path):
        """The age gate compares instants, so offset alone cannot buy freshness."""
        from datetime import timezone

        store, v = reconciled_store(tmp_path, qty="100")
        sgt = timezone(timedelta(hours=8))
        out = _sell(store, "sgt2", v, now=(DEFAULT_NOW + timedelta(days=30)).astimezone(sgt))
        assert out.blocked is True and out.block_reason == "stale snapshot"
        store.close()

    def test_offset_shifted_clock_cannot_forge_freshness(self, tmp_path):
        """A +08:00 clock whose *wall reading* looks recent but whose instant is
        30 days old must still be refused."""
        from datetime import timezone

        store, v = reconciled_store(tmp_path, qty="100")
        sgt = timezone(timedelta(hours=8))
        forged = (DEFAULT_NOW + timedelta(days=30)).replace(tzinfo=sgt)
        out = _sell(store, "forge", v, now=forged)
        assert out.blocked is True
        assert store.get_proposal("forge") is None
        store.close()

    def test_naive_captured_at_in_the_database_fails_closed(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        store._conn.execute(
            "UPDATE broker_snapshots SET captured_at = ? WHERE version = ?",
            (DEFAULT_NOW.replace(tzinfo=None).isoformat(), v),
        )
        p = make_proposal("nz", [make_order("nz", 0, "SGOV", Side.SELL, "10", SGOV)])
        outcome = None
        raised = None
        try:
            outcome = evaluate_and_reserve(
                store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES, expected_snapshot_version=v
            )
        except Exception as exc:  # noqa: BLE001
            raised = exc
        assert raised is not None or (outcome is not None and not outcome.is_auto)
        assert store.get_proposal("nz") is None
        store.close()


# =====================================================================  P1-1


class FakeReadOnlyTradingClient:
    """A paper TradingClient double that also carries every mutator.

    Never called: the probes assert unreachability, they do not invoke.
    """

    _base_url = "https://paper-api.alpaca.markets"
    _paper = True

    def __init__(self):
        self.mutations_called = []

    # --- read surface
    def get_account(self):
        return {"cash": "100000"}

    def get_all_positions(self):
        return ()

    def get_asset(self, symbol):
        return {"symbol": symbol}

    def get_orders(self, filter=None):
        return ()

    def get_order_by_client_id(self, client_order_id):
        return None

    def get_calendar(self, filters=None):
        return ()

    def get_clock(self):
        return {}

    # --- mutation surface (must never be reachable through the gateway)
    def submit_order(self, *a, **kw):
        self.mutations_called.append("submit_order")

    def cancel_order_by_id(self, *a, **kw):
        self.mutations_called.append("cancel_order_by_id")

    def cancel_orders(self, *a, **kw):
        self.mutations_called.append("cancel_orders")

    def replace_order_by_id(self, *a, **kw):
        self.mutations_called.append("replace_order_by_id")

    def close_position(self, *a, **kw):
        self.mutations_called.append("close_position")

    def close_all_positions(self, *a, **kw):
        self.mutations_called.append("close_all_positions")

    def exercise_options_position(self, *a, **kw):
        self.mutations_called.append("exercise_options_position")

    def post(self, *a, **kw):
        self.mutations_called.append("post")

    def delete(self, *a, **kw):
        self.mutations_called.append("delete")


MUTATORS = (
    "submit_order",
    "cancel_order",
    "cancel_order_by_id",
    "cancel_orders",
    "replace_order",
    "replace_order_by_id",
    "close_position",
    "close_all_positions",
    "exercise_options_position",
    "post",
    "put",
    "patch",
    "delete",
    "request",
)


def _paper_gateway_over_full_client():
    from opaca.broker.alpaca import AlpacaPaperGateway

    client = FakeReadOnlyTradingClient()
    return AlpacaPaperGateway(client), client


class TestP11ReadOnlyCapability:
    @pytest.mark.parametrize("name", MUTATORS)
    def test_no_mutator_is_reachable_on_the_gateway(self, name):
        gateway, client = _paper_gateway_over_full_client()
        assert getattr(gateway, name, None) is None
        assert not client.mutations_called

    def test_no_client_attribute_is_retained(self):
        gateway, client = _paper_gateway_over_full_client()
        for attribute in ("_client", "client", "_trading_client", "_api", "__dict__"):
            assert getattr(gateway, attribute, None) is None, attribute
        assert not hasattr(gateway, "__dict__"), "gateway must use __slots__"
        assert not client.mutations_called

    def test_the_guard_rejects_a_gateway_that_retains_a_mutable_client(self):
        class Leaky:
            def __init__(self, client):
                self._client = client

            def get_account(self):
                return {}

        with pytest.raises(InvalidBrokerStateError) as info:
            assert_read_only_gateway(Leaky(FakeReadOnlyTradingClient()))
        assert "mutable broker client" in str(info.value)

    @pytest.mark.parametrize("attribute", ["_client", "client", "_trading_client"])
    def test_the_guard_covers_each_conventional_client_attribute(self, attribute):
        class Leaky:
            pass

        leaky = Leaky()
        setattr(leaky, attribute, FakeReadOnlyTradingClient())
        with pytest.raises(InvalidBrokerStateError):
            assert_read_only_gateway(leaky)

    @pytest.mark.parametrize("name", MUTATORS)
    def test_the_guard_rejects_each_mutator_on_the_gateway_itself(self, name):
        class Sneaky(FakeAlpacaGateway):
            pass

        gateway = Sneaky(account={}, assets={})
        setattr(gateway, name, lambda *a, **kw: None)
        with pytest.raises(InvalidBrokerStateError):
            assert_read_only_gateway(gateway)

    def test_forbidden_set_covers_every_real_alpaca_mutator_and_http_verb(self):
        from opaca.broker.mutation import FORBIDDEN_BROKER_MUTATIONS

        required = {
            "submit_order", "cancel_order", "cancel_order_by_id", "cancel_orders",
            "replace_order", "replace_order_by_id", "close_position",
            "close_all_positions", "exercise_options_position",
            "post", "put", "patch", "delete", "request",
        }
        assert required <= FORBIDDEN_BROKER_MUTATIONS

    def test_gateway_read_calls_still_work(self):
        gateway, client = _paper_gateway_over_full_client()
        assert gateway.get_account() == {"cash": "100000"}
        assert gateway.get_positions() == ()
        assert gateway.endpoint.startswith("https://paper-api.alpaca.markets")
        assert not client.mutations_called

    def test_orchestrator_refuses_a_gateway_with_a_nested_mutable_client(self, tmp_path):
        from opaca.orchestration.reserve import read_reconcile_evaluate_reserve

        class Leaky(FakeAlpacaGateway):
            pass

        leaky = Leaky(
            account=account_payload(),
            positions=(position_payload(qty="100"),),
            assets=PHASE1_ASSETS,
        )
        leaky._client = FakeReadOnlyTradingClient()
        store = temp_store(tmp_path)
        p = make_proposal("x", [make_order("x", 0, "SGOV", Side.SELL, "1", SGOV)])
        recon, out = read_reconcile_evaluate_reserve(
            store, leaky, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES
        )
        assert recon.status is ReconciliationStatus.INVALID_BROKER_STATE
        assert out.is_auto is False
        store.close()


# =====================================================================  P1-2


def _account(**over):
    payload = dict(account_payload())
    payload.update(over)
    return payload


INVALID_BROKER_CASES = {
    # duplicate rows
    "duplicate_position_rows": dict(
        positions=(position_payload(qty="100"), position_payload(qty="100"))
    ),
    "duplicate_client_order_id": dict(
        positions=(position_payload(qty="100"),),
        open_orders=(order_payload("dup", status="new"), order_payload("dup", status="new")),
    ),
    "duplicate_broker_order_id": dict(
        positions=(position_payload(qty="100"),),
        open_orders=(
            order_payload("a", status="new", broker_id="X"),
            order_payload("b", status="new", broker_id="X"),
        ),
    ),
    # filled > quantity
    "filled_gt_quantity": dict(
        positions=(position_payload(qty="100"),),
        open_orders=(order_payload("pf", status="partially_filled", qty="10", filled_qty="50"),),
    ),
    "filled_gt_quantity_on_filled": dict(
        positions=(position_payload(qty="100"),),
        open_orders=(order_payload("f", status="filled", qty="10", filled_qty="11"),),
    ),
    # quantity_available > quantity
    "available_gt_quantity": dict(
        positions=(position_payload(qty="100", qty_available="150"),)
    ),
    # malformed cash
    "cash_missing": dict(account={k: v for k, v in account_payload().items() if k != "cash"}),
    "cash_nan": dict(account=_account(cash="NaN")),
    "cash_snan": dict(account=_account(cash="sNaN")),
    "cash_infinity": dict(account=_account(cash="Infinity")),
    "cash_negative": dict(account=_account(cash="-1")),
    "cash_text": dict(account=_account(cash="one hundred")),
    "cash_float": dict(account=_account(cash=100000.0)),
    "cash_bool": dict(account=_account(cash=True)),
    "cash_list": dict(account=_account(cash=["100000"])),
    "cash_huge": dict(account=_account(cash="1e30")),
    # invalid Decimal elsewhere
    "position_qty_nan": dict(positions=({**position_payload(), "qty": "NaN"},)),
    "position_qty_text": dict(positions=({**position_payload(), "qty": "ten"},)),
    "position_qty_float": dict(positions=({**position_payload(), "qty": 100.0},)),
    "position_qty_negative": dict(positions=({**position_payload(), "qty": "-1"},)),
    "order_qty_negative": dict(
        positions=(position_payload(qty="100"),),
        open_orders=(order_payload("n", status="new", qty="-5"),),
    ),
    "order_qty_float": dict(
        positions=(position_payload(qty="100"),),
        open_orders=({**order_payload("n", status="new"), "qty": 5.0},),
    ),
    # invalid identity / state
    "malformed_side": dict(
        positions=(position_payload(qty="100"),),
        open_orders=(order_payload("s", status="new", side="sideways"),),
    ),
    "missing_client_order_id": dict(
        positions=(position_payload(qty="100"),),
        open_orders=({k: v for k, v in order_payload("s").items() if k != "client_order_id"},),
    ),
    "empty_client_order_id": dict(
        positions=(position_payload(qty="100"),), open_orders=(order_payload(""),)
    ),
    "missing_position_symbol": dict(
        positions=({k: v for k, v in position_payload().items() if k != "symbol"},)
    ),
    "short_position": dict(positions=({**position_payload(), "side": "short"},)),
    "asset_status_unknown": None,  # handled separately
}


class TestP12InvalidBrokerState:
    @pytest.mark.parametrize(
        "name", sorted(k for k, v in INVALID_BROKER_CASES.items() if v is not None)
    )
    def test_corrupt_broker_payload_is_invalid_broker_state(self, tmp_path, name):
        store = temp_store(tmp_path)
        result = reconcile(store, gw(**INVALID_BROKER_CASES[name]), now=DEFAULT_NOW)
        assert result.status is ReconciliationStatus.INVALID_BROKER_STATE, (
            name,
            result.status,
            result.reasons,
        )
        assert result.snapshot is None
        assert store.latest_snapshot() is None
        assert AuditEventType.INVALID_BROKER_STATE in [
            e.event_type for e in store.list_audit()
        ]
        store.close()

    @pytest.mark.parametrize(
        "name", sorted(k for k, v in INVALID_BROKER_CASES.items() if v is not None)
    )
    def test_no_raw_exception_escapes_the_reconciliation_boundary(self, tmp_path, name):
        store = temp_store(tmp_path)
        try:
            reconcile(store, gw(**INVALID_BROKER_CASES[name]), now=DEFAULT_NOW)
        except BaseException as exc:  # noqa: BLE001
            pytest.fail(f"{name} raised {type(exc).__name__}: {exc}")
        finally:
            store.close()

    def test_unmapped_order_status_is_uncertainty_not_corruption(self, tmp_path):
        """An unknown Alpaca status is UNKNOWN_REQUIRES_REVIEW, not INVALID_BROKER_STATE:
        the payload is well formed, the meaning is not known."""
        store = temp_store(tmp_path)
        result = reconcile(
            store,
            gw(positions=(position_payload(qty="100"),),
               open_orders=(order_payload("u", status="frobnicated"),)),
            now=DEFAULT_NOW,
        )
        assert result.status is ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW
        store.close()

    def test_corrupt_unknown_order_row_is_invalid_broker_state(self, tmp_path):
        store = temp_store(tmp_path)
        with store.begin_immediate() as conn:
            store.upsert_unknown_order(
                UnknownOrderRecord(
                    client_order_id="bad", proposal_id="p", symbol="SGOV", side="SELL",
                    quantity=Decimal("10"), filled_quantity=Decimal("50"),
                    state=OrderState.PARTIALLY_FILLED.value,
                    last_lookup_at=None, created_at=DEFAULT_NOW,
                ),
                conn=conn,
            )
        result = reconcile(store, gw(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
        assert result.status is ReconciliationStatus.INVALID_BROKER_STATE
        assert result.snapshot is None
        store.close()

    def test_missing_asset_metadata_is_invalid_broker_state(self, tmp_path):
        store = temp_store(tmp_path)
        assets = {k: v for k, v in PHASE1_ASSETS.items() if k != "SGOV"}
        result = reconcile(store, gw(assets=assets), now=DEFAULT_NOW)
        assert result.status is ReconciliationStatus.INVALID_BROKER_STATE
        store.close()

    def test_broker_unavailable_stays_its_own_state(self, tmp_path):
        store = temp_store(tmp_path)
        result = reconcile(store, paper_gateway(unavailable=True), now=DEFAULT_NOW)
        assert result.status is ReconciliationStatus.BROKER_UNAVAILABLE
        store.close()

    @pytest.mark.parametrize(
        "bad_price",
        [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), 100.69, "100.69", None],
    )
    def test_malformed_price_never_produces_auto(self, tmp_path, bad_price):
        store, v = reconciled_store(tmp_path, qty="100")
        prices = dict(DEFAULT_PRICES)
        prices["SGOV"] = bad_price
        p = make_proposal("bp", [make_order("bp", 0, "SGOV", Side.SELL, "10", SGOV)])
        outcome = None
        try:
            outcome = evaluate_and_reserve(
                store, p, now=DEFAULT_NOW, prices=prices, expected_snapshot_version=v
            )
        except Exception:  # noqa: BLE001, S110
            pass
        assert outcome is None or outcome.is_auto is False
        assert store.get_proposal("bp") is None
        assert store.count_reservations("bp") == 0
        store.close()

    def test_missing_price_never_produces_auto(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        prices = {k: value for k, value in DEFAULT_PRICES.items() if k != "SGOV"}
        p = make_proposal("mp", [make_order("mp", 0, "SGOV", Side.SELL, "10", SGOV)])
        out = evaluate_and_reserve(
            store, p, now=DEFAULT_NOW, prices=prices, expected_snapshot_version=v
        )
        assert out.is_auto is False
        store.close()

    def test_corrupt_broker_state_after_a_good_snapshot_does_not_replace_it(self, tmp_path):
        """A failed reconcile must not overwrite or invalidate the last good snapshot,
        and must not leave a half-written one."""
        store, v = reconciled_store(tmp_path, qty="100")
        before = store.latest_snapshot()
        result = reconcile(store, gw(account=_account(cash="NaN")), now=DEFAULT_NOW)
        assert result.status is ReconciliationStatus.INVALID_BROKER_STATE
        after = store.latest_snapshot()
        assert after == before
        assert after.version == v
        store.close()


# =====================================================================  P1-3


def _escalated(tmp_path):
    store, v = reconciled_store(tmp_path, qty=None, cash="100000")
    store._conn.execute(
        "INSERT INTO autonomous_executions(proposal_id, timestamp, notional) VALUES (?,?,?)",
        ("prior", dump_datetime(DEFAULT_NOW), "49000"),
    )
    proposal = make_proposal("ar", [make_order("ar", 0, "SGOV", Side.BUY, "20", SGOV)])
    out = evaluate_and_reserve(
        store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES, expected_snapshot_version=v
    )
    assert out.authority_result is AuthorityResult.APPROVAL_REQUIRED
    assert out.expires_at == DEFAULT_NOW + timedelta(seconds=300)
    return store, proposal, out


def _refresh_snapshot(store, at):
    recon = reconcile(store, gw(), now=at)
    assert recon.status is ReconciliationStatus.RECONCILED, recon.reasons
    return recon.snapshot.version


class TestP13ApprovalExpiry:
    def test_expiry_is_recorded_on_the_result_and_the_record(self, tmp_path):
        store, _, out = _escalated(tmp_path)
        record = store.get_proposal("ar")
        assert record.expires_at == DEFAULT_NOW + timedelta(seconds=300)
        assert out.approval_currently_valid(DEFAULT_NOW) is True
        store.close()

    def test_replay_one_second_before_expiry_is_still_a_valid_approval(self, tmp_path):
        store, proposal, _ = _escalated(tmp_path)
        at = DEFAULT_NOW + timedelta(seconds=299)
        version = _refresh_snapshot(store, at)
        replay = evaluate_and_reserve(
            store, proposal, now=at, prices=DEFAULT_PRICES, expected_snapshot_version=version
        )
        assert replay.blocked is False
        assert replay.idempotent_replay is True
        assert replay.authority_result is AuthorityResult.APPROVAL_REQUIRED
        assert replay.approval_currently_valid(at) is True
        assert replay.is_auto is False
        store.close()

    def test_replay_exactly_at_expiry_is_expired(self, tmp_path):
        store, proposal, _ = _escalated(tmp_path)
        at = DEFAULT_NOW + timedelta(seconds=300)
        version = _refresh_snapshot(store, at)
        replay = evaluate_and_reserve(
            store, proposal, now=at, prices=DEFAULT_PRICES, expected_snapshot_version=version
        )
        assert replay.blocked is True
        assert replay.block_reason == "approval expired"
        assert replay.approval_currently_valid(at) is False
        assert replay.is_auto is False
        store.close()

    def test_replay_after_expiry_is_expired(self, tmp_path):
        store, proposal, _ = _escalated(tmp_path)
        at = DEFAULT_NOW + timedelta(seconds=301)
        version = _refresh_snapshot(store, at)
        replay = evaluate_and_reserve(
            store, proposal, now=at, prices=DEFAULT_PRICES, expected_snapshot_version=version
        )
        assert replay.blocked is True
        assert replay.block_reason == "approval expired"
        assert replay.approval_currently_valid(at) is False
        store.close()

    def test_expired_approval_is_audited_as_denied(self, tmp_path):
        store, proposal, _ = _escalated(tmp_path)
        at = DEFAULT_NOW + timedelta(seconds=600)
        version = _refresh_snapshot(store, at)
        evaluate_and_reserve(
            store, proposal, now=at, prices=DEFAULT_PRICES, expected_snapshot_version=version
        )
        denials = [
            e for e in store.list_audit(proposal_id="ar")
            if e.event_type is AuditEventType.RESERVATION_DENIED
        ]
        assert denials and any("approval expired" in e.reason for e in denials)
        store.close()

    def test_expired_approval_never_reserves_capacity(self, tmp_path):
        store, proposal, _ = _escalated(tmp_path)
        at = DEFAULT_NOW + timedelta(seconds=900)
        version = _refresh_snapshot(store, at)
        evaluate_and_reserve(
            store, proposal, now=at, prices=DEFAULT_PRICES, expected_snapshot_version=version
        )
        assert store.count_reservations("ar") == 0
        assert [h.notional for h in store.load_autonomous_history()] == [Decimal("49000")]
        store.close()

    def test_approval_validity_helper_rejects_non_approval_results(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        _, auto = _auto(store, v)
        assert auto.approval_currently_valid(DEFAULT_NOW) is False
        rejected = _sell(store, "big", v, qty="500")
        assert rejected.approval_currently_valid(DEFAULT_NOW) is False
        store.close()

    def test_human_approval_cannot_resurrect_a_hard_reject(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        out = _sell(store, "hard", v, qty="500")
        assert out.authority_result is AuthorityResult.REJECT
        record = store.get_proposal("hard")
        assert record.expires_at is None
        assert record.is_currently_valid_approval(DEFAULT_NOW) is False
        assert store._conn.execute(
            "SELECT COUNT(*) AS n FROM approvals WHERE proposal_id='hard'"
        ).fetchone()["n"] == 0
        store.close()


# =====================================================================  P1-4


def _reserved_sell(store, version, pid, qty):
    """Create a real AUTO sell reservation and return (proposal, client_order_id)."""
    proposal = make_proposal(pid, [make_order(pid, 0, "SGOV", Side.SELL, qty, SGOV)])
    out = evaluate_and_reserve(
        store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
        expected_snapshot_version=version,
    )
    assert out.is_auto
    return proposal, proposal.legs[0].client_order_id


def _known_broker_sell(store, client_order_id, qty, filled="0"):
    """Register a locally-known order identity so the broker order is not 'unknown locally'."""
    with store.begin_immediate() as conn:
        store.upsert_unknown_order(
            UnknownOrderRecord(
                client_order_id=client_order_id, proposal_id="local", symbol="SGOV",
                side="SELL", quantity=Decimal(qty), filled_quantity=Decimal(filled),
                state=OrderState.PARTIALLY_FILLED.value, last_lookup_at=None,
                created_at=DEFAULT_NOW,
            ),
            conn=conn,
        )


class TestP14QuantityAvailableDrift:
    def test_unexplained_reduction_is_drift(self, tmp_path):
        store = temp_store(tmp_path)
        reconcile(store, gw(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
        result = reconcile(
            store,
            gw(positions=(position_payload(qty="100", qty_available="10"),)),
            now=DEFAULT_NOW,
        )
        assert result.status is ReconciliationStatus.DRIFT_DETECTED
        assert any("unexplained hold-aside" in r for r in result.reasons), result.reasons
        store.close()

    def test_a_known_broker_sell_explains_the_hold_aside(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        _known_broker_sell(store, "b1", qty="20", filled="5")
        result = reconcile(
            store,
            gw(positions=(position_payload(qty="100", qty_available="85"),),
               open_orders=(order_payload("b1", status="partially_filled", qty="20",
                                          filled_qty="5"),)),
            now=DEFAULT_NOW,
        )
        assert result.status is ReconciliationStatus.RECONCILED, result.reasons
        store.close()

    def test_a_known_broker_sell_does_not_explain_more_than_its_remainder(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        _known_broker_sell(store, "b1", qty="20", filled="5")
        result = reconcile(
            store,
            gw(positions=(position_payload(qty="100", qty_available="84"),),
               open_orders=(order_payload("b1", status="partially_filled", qty="20",
                                          filled_qty="5"),)),
            now=DEFAULT_NOW,
        )
        assert result.status is ReconciliationStatus.DRIFT_DETECTED, result.reasons
        store.close()

    def test_a_local_reservation_explains_the_hold_aside(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        _reserved_sell(store, v, "r1", "10")
        result = reconcile(
            store,
            gw(positions=(position_payload(qty="100", qty_available="90"),)),
            now=DEFAULT_NOW,
        )
        assert result.status is ReconciliationStatus.RECONCILED, result.reasons
        store.close()

    def test_a_local_reservation_does_not_explain_more_than_it_reserves(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        _reserved_sell(store, v, "r1", "10")
        result = reconcile(
            store,
            gw(positions=(position_payload(qty="100", qty_available="89"),)),
            now=DEFAULT_NOW,
        )
        assert result.status is ReconciliationStatus.DRIFT_DETECTED, result.reasons
        store.close()

    def test_reservation_and_distinct_broker_sell_add_up_exactly(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        _reserved_sell(store, v, "r1", "10")
        _known_broker_sell(store, "b1", qty="20", filled="5")
        # 10 reserved + 15 remaining = 25 explained
        ok = reconcile(
            store,
            gw(positions=(position_payload(qty="100", qty_available="75"),),
               open_orders=(order_payload("b1", status="partially_filled", qty="20",
                                          filled_qty="5"),)),
            now=DEFAULT_NOW,
        )
        assert ok.status is ReconciliationStatus.RECONCILED, ok.reasons
        drift = reconcile(
            store,
            gw(positions=(position_payload(qty="100", qty_available="74"),),
               open_orders=(order_payload("b1", status="partially_filled", qty="20",
                                          filled_qty="5"),)),
            now=DEFAULT_NOW,
        )
        assert drift.status is ReconciliationStatus.DRIFT_DETECTED, drift.reasons
        store.close()

    def test_the_same_order_seen_twice_is_not_double_subtracted(self, tmp_path):
        """A reservation and the broker order it produced share a client_order_id.
        The hold-aside they explain is 10, not 20."""
        store, v = reconciled_store(tmp_path, qty="100")
        _, client_order_id = _reserved_sell(store, v, "r1", "10")
        ok = reconcile(
            store,
            gw(positions=(position_payload(qty="100", qty_available="90"),),
               open_orders=(order_payload(client_order_id, status="new", qty="10",
                                          filled_qty="0"),)),
            now=DEFAULT_NOW,
        )
        assert ok.status is ReconciliationStatus.RECONCILED, ok.reasons
        doubled = reconcile(
            store,
            gw(positions=(position_payload(qty="100", qty_available="80"),),
               open_orders=(order_payload(client_order_id, status="new", qty="10",
                                          filled_qty="0"),)),
            now=DEFAULT_NOW,
        )
        assert doubled.status is ReconciliationStatus.DRIFT_DETECTED, (
            "the same order counted twice would have explained a 20-share hold-aside"
        )
        store.close()

    def test_undeterminable_broker_sell_forces_review_not_reconciled(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        _known_broker_sell(store, "b1", qty="20", filled="0")
        result = reconcile(
            store,
            gw(positions=(position_payload(qty="100", qty_available="50"),),
               open_orders=(order_payload("b1", status="new", qty=None, filled_qty=None),)),
            now=DEFAULT_NOW,
        )
        assert result.status is ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW, result.reasons
        store.close()

    def test_quantity_available_greater_than_quantity_is_invalid(self, tmp_path):
        store = temp_store(tmp_path)
        result = reconcile(
            store,
            gw(positions=(position_payload(qty="100", qty_available="101"),)),
            now=DEFAULT_NOW,
        )
        assert result.status is ReconciliationStatus.INVALID_BROKER_STATE
        assert result.snapshot is None
        store.close()

    def test_no_hold_aside_reconciles_cleanly(self, tmp_path):
        store = temp_store(tmp_path)
        result = reconcile(store, gw(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
        assert result.status is ReconciliationStatus.RECONCILED
        assert not any("hold-aside" in r for r in result.reasons)
        store.close()


# =====================================================================  P1-5


class TestP15ScenarioSeedTransaction:
    def test_first_seed_writes_scenario_and_obligations_together(self, tmp_path):
        store = temp_store(tmp_path)
        seed = store.seed_scenario_once(Decimal("99999.99"), DEFAULT_NOW.date(), now=DEFAULT_NOW)
        assert seed.opening_cash == Decimal("99999.99")
        assert store._conn.execute(
            "SELECT COUNT(*) AS n FROM scenario_state").fetchone()["n"] == 1
        assert store._conn.execute(
            "SELECT COUNT(*) AS n FROM obligations").fetchone()["n"] == 2
        store.close()

    def test_repeat_seed_is_idempotent(self, tmp_path):
        store = temp_store(tmp_path)
        first = store.seed_scenario_once(Decimal("99999.99"), DEFAULT_NOW.date(), now=DEFAULT_NOW)
        for cash in ("1", "500000", "99999.99"):
            again = store.seed_scenario_once(Decimal(cash), DEFAULT_NOW.date(), now=DEFAULT_NOW)
            assert again == first
        assert store._conn.execute(
            "SELECT COUNT(*) AS n FROM obligations").fetchone()["n"] == 2
        assert store._conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events WHERE event_type='SCENARIO_SEEDED'"
        ).fetchone()["n"] == 1
        store.close()

    def test_mid_seed_failure_rolls_back_scenario_and_obligations_together(self, tmp_path):
        """The exact attack that succeeded at 3fdabf3: a colliding obligation id.
        The scenario row must not survive its failed obligations."""
        store = temp_store(tmp_path)
        store._conn.execute(
            "INSERT INTO obligations(obligation_id, name, amount, due_date, seeded) "
            "VALUES ('seed-payroll','squatter','1','2026-09-11',0)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.seed_scenario_once(Decimal("99999.99"), DEFAULT_NOW.date(), now=DEFAULT_NOW)
        assert store.get_scenario() is None
        assert store._conn.execute(
            "SELECT COUNT(*) AS n FROM scenario_state").fetchone()["n"] == 0
        assert store._conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events WHERE event_type='SCENARIO_SEEDED'"
        ).fetchone()["n"] == 0
        store.close()

    def test_seed_is_recoverable_after_a_failed_attempt(self, tmp_path):
        store = temp_store(tmp_path)
        store._conn.execute(
            "INSERT INTO obligations(obligation_id, name, amount, due_date, seeded) "
            "VALUES ('seed-payroll','squatter','1','2026-09-11',0)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.seed_scenario_once(Decimal("99999.99"), DEFAULT_NOW.date(), now=DEFAULT_NOW)
        store._conn.execute("DELETE FROM obligations WHERE obligation_id='seed-payroll'")
        seed = store.seed_scenario_once(Decimal("150000"), DEFAULT_NOW.date(), now=DEFAULT_NOW)
        assert seed.opening_cash == Decimal("150000")
        assert store.get_scenario().opening_cash == Decimal("150000")
        store.close()

    def test_injected_failure_inside_the_seed_transaction_leaves_nothing(self, tmp_path):
        store = temp_store(tmp_path)
        original = store.record_audit

        def failing(event_type, *a, **kw):
            if event_type.value == "SCENARIO_SEEDED":
                raise RuntimeError("injected mid-seed failure")
            return original(event_type, *a, **kw)

        store.record_audit = failing  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            store.seed_scenario_once(Decimal("99999.99"), DEFAULT_NOW.date(), now=DEFAULT_NOW)
        store.record_audit = original  # type: ignore[method-assign]
        assert store.get_scenario() is None
        assert store._conn.execute(
            "SELECT COUNT(*) AS n FROM obligations").fetchone()["n"] == 0
        store.close()

    def test_concurrent_initialization_produces_exactly_one_seed(self, tmp_path):
        import threading

        store = temp_store(tmp_path)
        path = store.path
        store.close()
        errors = []
        barrier = threading.Barrier(4)

        def worker(cash):
            try:
                local = SQLiteStore(path, timeout=15.0)
                barrier.wait(timeout=15)
                local.seed_scenario_once(Decimal(cash), DEFAULT_NOW.date(), now=DEFAULT_NOW)
                local.close()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(c,))
            for c in ("99999.99", "150000", "500000", "80000")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors, errors
        check = SQLiteStore(path)
        try:
            assert check._conn.execute(
                "SELECT COUNT(*) AS n FROM scenario_state").fetchone()["n"] == 1
            assert check._conn.execute(
                "SELECT COUNT(*) AS n FROM obligations").fetchone()["n"] == 2
            assert check._conn.execute(
                "SELECT COUNT(*) AS n FROM audit_events WHERE event_type='SCENARIO_SEEDED'"
            ).fetchone()["n"] == 1
        finally:
            check.close()

    def test_reopen_and_later_cash_never_reseed(self, tmp_path):
        store = temp_store(tmp_path)
        recon = reconcile(store, paper_gateway(cash="99999.99"), now=DEFAULT_NOW)
        assert recon.status is ReconciliationStatus.RECONCILED
        seed = store.get_scenario()
        path = store.path
        store.close()
        for cash in ("80000", "150000", "500000"):
            again = SQLiteStore(path)
            reconcile(again, paper_gateway(cash=cash), now=DEFAULT_NOW)
            assert again.get_scenario() == seed
            again.close()
        check = SQLiteStore(path)
        assert check._conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events WHERE event_type='SCENARIO_SEEDED'"
        ).fetchone()["n"] == 1
        check.close()


# ==========================================================  SPOT-CHECKS
#
# The full Phase 2 invariant suite still runs unchanged alongside this file.
# These are the named critical invariants, re-asserted here so a remediation
# regression shows up in the retest file itself.


class TestSpotChecks:
    def test_sixty_plus_sixty_sell_concurrency_against_one_hundred(self, tmp_path):
        from probe_support import reserved_totals, run_parallel, sell_worker

        store, version = reconciled_store(tmp_path, qty="100")
        path = store.path
        store.close()
        results, errors = run_parallel(
            [sell_worker(path, "a", "60", version), sell_worker(path, "b", "60", version)]
        )
        assert not errors, errors
        autos = [r for r in results if r is not None and r.is_auto]
        assert len(autos) == 1
        check = SQLiteStore(path)
        try:
            assert reserved_totals(check).get("SGOV", Decimal("0")) == Decimal("60")
            loser = [r for r in results if not r.is_auto][0]
            assert check.count_reservations(loser.proposal_id) == 0
            assert len(check.load_autonomous_history()) == 1
        finally:
            check.close()

    def test_fifteen_k_plus_fifteen_k_buy_concurrency_against_twenty_two_k(self, tmp_path):
        from probe_support import buy_worker, reserved_cash, run_parallel

        store, version = reconciled_store(tmp_path, qty=None, cash="100000")
        scenario = store.get_scenario()
        limit = (
            store.latest_snapshot().broker.cash
            - scenario.operating_reserve
            - scenario.obligations_total
        )
        assert limit == Decimal("22000")
        path = store.path
        store.close()
        results, errors = run_parallel(
            [buy_worker(path, "a", "149", version), buy_worker(path, "b", "149", version)]
        )
        assert not errors, errors
        check = SQLiteStore(path)
        try:
            assert reserved_cash(check) <= limit
            assert len([r for r in results if r is not None and r.is_auto]) == 1
        finally:
            check.close()

    def test_stale_snapshot_denial(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        reconcile(store, gw(positions=(position_payload(qty="100"),)), now=DEFAULT_NOW)
        out = _sell(store, "stale", v)
        assert out.blocked is True and out.reserved is False
        assert store.get_proposal("stale") is None
        store.close()

    def test_idempotency_is_capacity_neutral(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        proposal, _ = _auto(store, v, pid="idem")
        before = (len(store.load_autonomous_history()), store.count_reservations("idem"))
        for _ in range(5):
            evaluate_and_reserve(
                store, proposal, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                expected_snapshot_version=v,
            )
        assert (len(store.load_autonomous_history()), store.count_reservations("idem")) == before
        store.close()

    def test_rollback_atomicity(self, tmp_path):
        store, v = reconciled_store(tmp_path, qty="100")
        original = store.persist_reservations

        def boom(**kw):
            original(**kw)
            raise RuntimeError("injected")

        store.persist_reservations = boom  # type: ignore[method-assign]
        p = make_proposal("rb", [make_order("rb", 0, "SGOV", Side.SELL, "10", SGOV)])
        with pytest.raises(RuntimeError):
            evaluate_and_reserve(
                store, p, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
                expected_snapshot_version=v,
            )
        store.persist_reservations = original  # type: ignore[method-assign]
        assert store.get_proposal("rb") is None
        assert store.count_reservations("rb") == 0
        assert store.active_reservations() == ()
        assert store.load_autonomous_history() == ()
        store.close()

    def test_reconciliation_fails_closed_on_every_non_reconciled_status(self, tmp_path):
        store, _ = reconciled_store(tmp_path, qty="100")
        for gateway, expected in (
            (paper_gateway(unavailable=True), ReconciliationStatus.BROKER_UNAVAILABLE),
            (gw(account=_account(cash="NaN")), ReconciliationStatus.INVALID_BROKER_STATE),
            (
                gw(positions=(position_payload(qty="100"),),
                   open_orders=(order_payload("g", status="new"),)),
                ReconciliationStatus.DRIFT_DETECTED,
            ),
            (
                gw(positions=(position_payload(qty="100"),),
                   open_orders=(order_payload("h", status="frobnicated"),)),
                ReconciliationStatus.UNKNOWN_REQUIRES_REVIEW,
            ),
        ):
            result = reconcile(store, gateway, now=DEFAULT_NOW)
            assert result.status is expected, (expected, result.status, result.reasons)
            version = None if result.snapshot is None else result.snapshot.version
            out = _sell(store, f"blocked-{expected.value}", version)
            assert out.is_auto is False
            assert out.blocked is True
        store.close()
