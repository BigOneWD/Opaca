"""Final narrow remediation retest @ cd3dc86 — the five Phase 3 findings.

Every test asserts the FIXED behaviour and fails at 79a7b1b.
No live call and no broker mutation of any kind is performed.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from opaca.domain.models import Side
from opaca.execution.service import (
    execute_reserved_proposal,
    recover_proposal,
)
from opaca.orchestration.context import build_policy_context
from opaca.persistence.store import SQLiteStore
from opaca.persistence.types import (
    AuditEventType,
    ExecutionState,
    ReconciliationStatus,
    UnknownOrderRecord,
)
from opaca.policy.engine import effective_available_quantity, sell_reservations
from opaca.reconciliation.service import compare_state, reconcile

from p3_support import (
    DEFAULT_NOW,
    DEFAULT_PRICES,
    SGOV,
    World,
    active_cash,
    active_sell_qty,
    buy,
    make_order,
    make_proposal,
    reserve,
    sell,
    world,
)
from tests.state_helpers import account_payload, position_payload


def _execute(w, proposal, gw=None, now=DEFAULT_NOW, **kw):
    return execute_reserved_proposal(
        w.store, w.read(), gw if gw is not None else w.mutate(**kw),
        proposal, now=now, prices=DEFAULT_PRICES,
    )


def _capacity(w, symbol: str = "SGOV") -> Decimal:
    """The reservation-aware bound CHECK-16 actually uses."""
    context, _ = build_policy_context(w.store, now=DEFAULT_NOW, prices=DEFAULT_PRICES)
    reserved, undeterminable = sell_reservations(context.unresolved_orders)
    position = next((p for p in context.positions if p.symbol == symbol), None)
    return effective_available_quantity(
        position, reserved.get(symbol, Decimal("0")), symbol in undeterminable
    )


# =====================================================  1. P1-2 DOUBLE COUNT


class TestP12DoubleCount:
    def test_matching_reservation_and_broker_order_leaves_ninety(self, tmp_path):
        """position 100, our reservation 10 and the broker SELL 10 it produced:
        one economic commitment, so remaining capacity is 90 - not 80."""
        w = world(tmp_path, qty="100")
        proposal = sell("s1", "10")
        reserve(w, proposal)
        result = _execute(w, proposal, fill_on_submit=False)
        assert result.state is ExecutionState.SUBMITTED
        assert active_sell_qty(w.store) == Decimal("10")
        assert reconcile(w.store, w.read(), now=DEFAULT_NOW).status is (
            ReconciliationStatus.RECONCILED
        )
        assert _capacity(w) == Decimal("90")
        honest = sell("s2", "90")
        _, out = reserve(w, honest)
        assert out.is_auto, "the honest 90 must be authorised"
        w.close()

    def test_external_order_plus_independent_reservation_leaves_eighty(self, tmp_path):
        """Two distinct commitments must still be counted twice: an external broker
        SELL 10 that is not ours, plus our own independent reservation of 10."""
        store = SQLiteStore(tmp_path / "opaca.sqlite")
        w = World(
            store=store,
            account=dict(account_payload(cash="100000")),
            positions=[dict(position_payload(qty="100"))],
            open_orders=[{
                "id": "ext-1", "client_order_id": "someone-elses-order", "symbol": "SGOV",
                "side": "sell", "status": "new", "qty": "10", "filled_qty": "0",
            }],
        )
        # the external order is unknown locally: reconciliation flags it, as it should
        first = reconcile(store, w.read(), now=DEFAULT_NOW)
        assert first.status is ReconciliationStatus.DRIFT_DETECTED
        # register it as a known local identity so the account is tradable again,
        # while keeping it a DISTINCT commitment from our own reservation
        with store.begin_immediate() as conn:
            store.upsert_unknown_order(
                UnknownOrderRecord(
                    client_order_id="someone-elses-order", proposal_id="external",
                    symbol="SGOV", side="SELL", quantity=Decimal("10"),
                    filled_quantity=Decimal("0"),
                    state="PARTIALLY_FILLED", last_lookup_at=None, created_at=DEFAULT_NOW,
                ),
                conn=conn,
            )
        assert reconcile(store, w.read(), now=DEFAULT_NOW).status is (
            ReconciliationStatus.RECONCILED
        )
        assert _capacity(w) == Decimal("90"), "the external order alone encumbers 10"
        proposal = sell("mine", "10")
        _, out = reserve(w, proposal)
        assert out.is_auto
        assert _capacity(w) == Decimal("80"), "two distinct commitments encumber 20"
        w.close()

    def test_a_matching_partial_fill_is_counted_once(self, tmp_path):
        """60 ordered, 20 filled: 80 shares held, 40 genuinely encumbered."""
        w = world(tmp_path, qty="100")
        proposal = sell("s1", "60")
        reserve(w, proposal)
        _execute(w, proposal, partial_fill_qty=Decimal("20"))
        assert active_sell_qty(w.store) == Decimal("40")
        assert Decimal(str(w.positions[0]["qty"])) == Decimal("80")
        assert reconcile(w.store, w.read(), now=DEFAULT_NOW).status is (
            ReconciliationStatus.RECONCILED
        )
        assert _capacity(w) == Decimal("40"), "the economic commitment is 40, counted once"
        ok = sell("s2", "40")
        _, out = reserve(w, ok)
        assert out.is_auto
        w.close()

    def test_an_undeterminable_identity_fails_closed(self, tmp_path):
        """An unresolved SELL whose remaining quantity cannot be determined must
        drive capacity to zero, never to an over-permissive number."""
        w = world(tmp_path, qty="100")
        assert _capacity(w) == Decimal("100")
        with w.store.begin_immediate() as conn:
            w.store.upsert_unknown_order(
                UnknownOrderRecord(
                    client_order_id="ghost-order", proposal_id="external", symbol="SGOV",
                    side="SELL", quantity=None, filled_quantity=None,
                    state="PARTIALLY_FILLED", last_lookup_at=None, created_at=DEFAULT_NOW,
                ),
                conn=conn,
            )
        assert _capacity(w) == Decimal("0"), "undeterminable size must fail closed"
        proposal = sell("s1", "1")
        _, out = reserve(w, proposal)
        assert not out.is_auto, "no sell of any size may be authorised"
        w.close()

    def test_an_unknown_execution_never_permits_an_oversell(self, tmp_path):
        w = world(tmp_path, qty="100")
        proposal = sell("s1", "60")
        reserve(w, proposal)
        _execute(w, proposal, timeout_before_accept=True)
        assert active_sell_qty(w.store) == Decimal("60"), "UNKNOWN retains capacity"
        assert _capacity(w) <= Decimal("40")
        w.close()

    def test_deterministic_correlation_survives_a_restart(self, tmp_path):
        """Close the store, reopen it, and the live order must still correlate to its
        reservation by deterministic client_order_id - not be counted a second time."""
        from opaca.policy.client_order_id import deterministic_client_order_id

        w = world(tmp_path, qty="100")
        proposal = sell("s1", "10")
        reserve(w, proposal)
        coid = proposal.legs[0].client_order_id
        assert coid == deterministic_client_order_id("s1", 0)
        _execute(w, proposal, fill_on_submit=False)
        assert reconcile(w.store, w.read(), now=DEFAULT_NOW).status is (
            ReconciliationStatus.RECONCILED
        )
        before = _capacity(w)
        path = w.store.path
        w.store.close()

        w.store = SQLiteStore(path)
        assert w.store.get_execution_order(coid) is not None
        assert _capacity(w) == before == Decimal("90")
        assert reconcile(w.store, w.read(), now=DEFAULT_NOW).status is (
            ReconciliationStatus.RECONCILED
        )
        assert _capacity(w) == Decimal("90"), "correlation must survive the restart"
        w.close()


# =====================================================  2. P1-1 ZERO-DELTA


def _snapshot_case(prev_qty, curr_qty, explained_orders):
    from opaca.domain.models import BrokerCashState, Position
    from opaca.persistence.types import PersistedSnapshot

    broker = BrokerCashState(
        cash=Decimal("100000"), buying_power=Decimal("400000"),
        non_marginable_buying_power=Decimal("100000"), multiplier=Decimal("4"),
        as_of=DEFAULT_NOW,
    )

    def pos(q):
        return Position(symbol="SGOV", quantity=Decimal(q), quantity_available=Decimal(q),
                        market_value=Decimal(q) * SGOV)

    previous = PersistedSnapshot(
        snapshot_id=1, version=1, broker=broker, positions=(pos(prev_qty),), assets=(),
        orders=(), reconciliation_status=ReconciliationStatus.RECONCILED,
        captured_at=DEFAULT_NOW, diagnostics="{}",
    )
    return compare_state(
        broker=broker, positions=(pos(curr_qty),), orders=(), previous=previous,
        reservations=(), unknown_orders=(), settlement_events=(),
        as_of=DEFAULT_NOW.date(), execution_orders=explained_orders,
    )


def _exec_order(filled, reconciled="0", side=Side.SELL, coid="opaca-" + "0" * 32):
    from opaca.persistence.types import ExecutionOrderRecord

    return ExecutionOrderRecord(
        client_order_id=coid, proposal_id="s1", leg_index=0, symbol="SGOV", side=side,
        quantity=Decimal("10"), filled_quantity=Decimal(filled),
        remaining_quantity=Decimal("10") - Decimal(filled), state=ExecutionState.FILLED,
        broker_order_id="b1", last_broker_status="filled", filled_avg_price=SGOV,
        reference_price=SGOV, reconciled_filled_quantity=Decimal(reconciled),
        settled_proceeds=Decimal("0"), created_at=DEFAULT_NOW, updated_at=DEFAULT_NOW,
    )


class TestP11ZeroDelta:
    def test_real_no_change_reconciles(self):
        status, reasons = _snapshot_case("100", "100", ())
        assert status is ReconciliationStatus.RECONCILED, reasons

    def test_an_expected_fill_that_is_reflected_reconciles(self):
        status, reasons = _snapshot_case("100", "90", (_exec_order("10"),))
        assert status is ReconciliationStatus.RECONCILED, reasons

    def test_an_expected_fill_that_is_absent_is_drift(self):
        """The exact hole closed here: delta 0 with an explained -10."""
        status, reasons = _snapshot_case("100", "100", (_exec_order("10"),))
        assert status is ReconciliationStatus.DRIFT_DETECTED
        assert any("not reflected in broker position" in r for r in reasons), reasons

    def test_a_fill_exactly_offset_by_external_movement_is_drift(self, tmp_path):
        w = world(tmp_path, qty="100")
        proposal = sell("s1", "10")
        reserve(w, proposal)
        _execute(w, proposal)
        w.positions[0]["qty"] = "100"          # an external +10 restores the position
        w.positions[0]["qty_available"] = "100"
        recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
        assert recon.status is ReconciliationStatus.DRIFT_DETECTED, recon.reasons
        w.close()

    def test_a_duplicate_broker_snapshot_is_idempotent(self, tmp_path):
        """Reconciling twice against identical broker state must stay RECONCILED and
        must not let the same fill explain a second delta."""
        w = world(tmp_path, qty="100")
        proposal = sell("s1", "10")
        reserve(w, proposal)
        _execute(w, proposal)
        for _ in range(3):
            assert reconcile(w.store, w.read(), now=DEFAULT_NOW).status is (
                ReconciliationStatus.RECONCILED
            )
        w.positions[0]["qty"] = "80"           # a second, unexplained -10
        w.positions[0]["qty_available"] = "80"
        recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
        assert recon.status is ReconciliationStatus.DRIFT_DETECTED, recon.reasons
        w.close()

    def test_progressive_partial_fills_each_reconcile(self, tmp_path):
        w = world(tmp_path, qty="100")
        proposal = sell("s1", "30")
        reserve(w, proposal)
        gw = w.mutate(partial_fill_qty=Decimal("10"))
        _execute(w, proposal, gw)
        assert reconcile(w.store, w.read(), now=DEFAULT_NOW).status is (
            ReconciliationStatus.RECONCILED
        )
        coid = proposal.legs[0].client_order_id
        for filled, qty in (("20", "80"), ("30", "70")):
            w.orders[coid]["filled_qty"] = filled
            w.orders[coid]["status"] = "filled" if filled == "30" else "partially_filled"
            w.positions[0]["qty"] = qty
            w.positions[0]["qty_available"] = qty
            recover_proposal(w.store, w.read(), "s1", now=DEFAULT_NOW)
            recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
            assert recon.status is ReconciliationStatus.RECONCILED, (filled, recon.reasons)
        assert active_sell_qty(w.store) == Decimal("0")
        w.close()

    def test_a_zero_delta_alone_does_not_imply_reconciled(self):
        """The invariant in one line."""
        assert _snapshot_case("100", "100", ())[0] is ReconciliationStatus.RECONCILED
        assert _snapshot_case("100", "100", (_exec_order("10"),))[0] is (
            ReconciliationStatus.DRIFT_DETECTED
        )


# =====================================================  3. P1-3 STRANDED LEGS


def _three_leg_world(tmp_path):
    store = SQLiteStore(tmp_path / "opaca.sqlite")
    w = World(
        store=store,
        account=dict(account_payload(cash="100000")),
        positions=[
            dict(position_payload(qty="100")),
            {"symbol": "BIL", "side": "long", "qty": "50", "qty_available": "50",
             "market_value": "4600"},
            {"symbol": "SHV", "side": "long", "qty": "40", "qty_available": "40",
             "market_value": "4400"},
        ],
    )
    assert reconcile(store, w.read(), now=DEFAULT_NOW).status is (
        ReconciliationStatus.RECONCILED
    )
    proposal = make_proposal("m1", [
        make_order("m1", 0, "SGOV", Side.SELL, "5", SGOV),
        make_order("m1", 1, "BIL", Side.SELL, "2", DEFAULT_PRICES["BIL"]),
        make_order("m1", 2, "SHV", Side.SELL, "1", DEFAULT_PRICES["SHV"]),
    ])
    _, out = reserve(w, proposal)
    assert out.is_auto, out.authority_result
    return w, proposal


class TestP13StrandedLegs:
    @pytest.mark.parametrize("rejected_leg", [1, 2, 3])
    def test_a_rejected_leg_marks_later_legs_not_submitted(self, tmp_path, rejected_leg):
        w, proposal = _three_leg_world(tmp_path)
        gw = w.mutate(reject_on_call=rejected_leg)
        _execute(w, proposal, gw)
        assert gw.submit_calls == rejected_leg, "no leg after the rejection is submitted"
        rows = {r.leg_index: r for r in w.store.list_execution_orders(proposal_id="m1")}
        assert rows[rejected_leg - 1].state is ExecutionState.REJECTED
        for index in range(rejected_leg, 3):
            stranded = rows[index]
            assert stranded.state is ExecutionState.NOT_SUBMITTED, index
            assert stranded.state is not ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
            assert stranded.broker_order_id is None
        w.close()

    def test_the_abort_reason_is_preserved_in_the_audit(self, tmp_path):
        w, proposal = _three_leg_world(tmp_path)
        _execute(w, proposal, reject_on_call=1)
        events = [
            e for e in w.store.list_audit(proposal_id="m1")
            if e.event_type is AuditEventType.ORDER_NOT_SUBMITTED
        ]
        assert len(events) == 2
        assert all("rejected by broker" in e.reason for e in events), [e.reason for e in events]
        assert {e.broker_identifiers for e in events} == {
            proposal.legs[1].client_order_id, proposal.legs[2].client_order_id
        }
        w.close()

    def test_reservations_are_released_after_a_definite_abort(self, tmp_path):
        w, proposal = _three_leg_world(tmp_path)
        _execute(w, proposal, reject_on_call=1)
        assert active_sell_qty(w.store, "SGOV") == Decimal("0")
        assert active_sell_qty(w.store, "BIL") == Decimal("0")
        assert active_sell_qty(w.store, "SHV") == Decimal("0")
        w.close()

    def test_a_final_leg_rejection_strands_nothing(self, tmp_path):
        w, proposal = _three_leg_world(tmp_path)
        gw = w.mutate(reject_on_call=3)
        _execute(w, proposal, gw)
        rows = {r.leg_index: r for r in w.store.list_execution_orders(proposal_id="m1")}
        assert rows[0].state is ExecutionState.FILLED
        assert rows[1].state is ExecutionState.FILLED
        assert rows[2].state is ExecutionState.REJECTED
        assert not any(
            r.state is ExecutionState.NOT_SUBMITTED for r in rows.values()
        )
        w.close()

    def test_uncertainty_keeps_unknown_and_retains_reservations(self, tmp_path):
        """The contrast case: a lost response is NOT a definite abort."""
        w, proposal = _three_leg_world(tmp_path)
        gw = w.mutate(timeout_after_accept=True)
        _execute(w, proposal, gw)
        rows = {r.leg_index: r for r in w.store.list_execution_orders(proposal_id="m1")}
        assert rows[0].state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
        assert rows[1].state is ExecutionState.NOT_SUBMITTED
        assert rows[2].state is ExecutionState.NOT_SUBMITTED
        assert active_sell_qty(w.store, "SGOV") == Decimal("5"), "UNKNOWN retains capacity"
        assert active_sell_qty(w.store, "BIL") == Decimal("2")
        w.close()

    def test_not_submitted_is_terminal_and_never_resubmits(self, tmp_path):
        w, proposal = _three_leg_world(tmp_path)
        gw = w.mutate(reject_on_call=1)
        _execute(w, proposal, gw)
        for _ in range(3):
            again = _execute(w, proposal, gw)
            assert again.submitted is False
        assert gw.submit_calls == 1
        w.close()

    def test_not_submitted_is_absorbing_in_the_state_machine(self):
        from opaca.execution.states import LEGAL_TRANSITIONS, TERMINAL_STATES
        from opaca.execution.errors import IllegalTransitionError
        from opaca.execution.states import validate_transition

        assert ExecutionState.NOT_SUBMITTED in TERMINAL_STATES
        assert LEGAL_TRANSITIONS[ExecutionState.NOT_SUBMITTED] == frozenset()
        for target in ExecutionState:
            if target is ExecutionState.NOT_SUBMITTED:
                continue
            with pytest.raises(IllegalTransitionError):
                validate_transition(ExecutionState.NOT_SUBMITTED, target)

    def test_a_leg_that_reached_the_broker_can_never_be_marked_not_submitted(self, tmp_path):
        from opaca.execution.errors import ExecutionInvariantError
        from opaca.execution.service import _mark_not_submitted

        w = world(tmp_path, qty="100")
        proposal = sell("s1", "10")
        reserve(w, proposal)
        _execute(w, proposal, fill_on_submit=False)
        with pytest.raises(ExecutionInvariantError):
            _mark_not_submitted(
                w.store, proposal.legs[0].client_order_id, now=DEFAULT_NOW,
                reason="probe: must be refused",
            )
        w.close()


# =====================================================  4. P2-1 BARE ASSERT


class TestP21NoBareAssert:
    def test_zero_ast_assert_in_every_production_module(self):
        import ast
        import pathlib

        import opaca

        root = pathlib.Path(opaca.__file__).resolve().parent
        modules = sorted(root.rglob("*.py"))
        found = [
            f"{p.relative_to(root)}:{n.lineno}"
            for p in modules
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
            if isinstance(n, ast.Assert)
        ]
        assert modules, "probe must actually scan something"
        assert found == [], found

    def test_the_replacement_is_a_real_raise(self, tmp_path):
        from opaca.domain.models import Proposal
        from opaca.orchestration.reserve import evaluate_and_reserve

        w = world(tmp_path)
        empty = Proposal(proposal_id="empty", legs=())
        recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
        evaluate_and_reserve(
            w.store, empty, now=DEFAULT_NOW, prices=DEFAULT_PRICES,
            expected_snapshot_version=recon.snapshot.version,
        )
        from opaca.execution.errors import ExecutionInvariantError

        gw = w.mutate()
        outcome = None
        raised = None
        try:
            outcome = _execute(w, empty, gw)
        except ExecutionInvariantError as exc:
            raised = exc
        w.close()
        assert gw.submit_calls == 0
        assert raised is not None or (outcome is not None and outcome.blocked)


# =====================================================  5. P3-1 KILL SWITCH


class TestP31KillSwitch:
    def test_a_flip_before_revalidation_submits_nothing(self, tmp_path):
        w = world(tmp_path)
        proposal = sell("s1", "10")
        reserve(w, proposal)
        w.store.set_kill_switch(True, now=DEFAULT_NOW)
        gw = w.mutate()
        result = _execute(w, proposal, gw)
        assert gw.submit_calls == 0
        assert result.blocked is True
        assert result.block_reason == "kill switch active"
        assert w.store.list_execution_orders(proposal_id="s1") == ()
        w.close()

    def test_a_flip_after_reconcile_but_before_the_intent_txn_submits_nothing(self, tmp_path):
        """B: the in-transaction check catches it, and no intent is persisted."""
        w = world(tmp_path)
        proposal = sell("s1", "10")
        reserve(w, proposal)
        gw = w.mutate()
        original = w.store.get_proposal
        flipped = {"done": False}

        def flip_then_read(*a, **kw):
            if not flipped["done"]:
                flipped["done"] = True
                w.store._conn.execute(
                    "UPDATE system_state SET value='1' WHERE key='kill_switch'"
                )
            return original(*a, **kw)

        w.store.get_proposal = flip_then_read  # type: ignore[method-assign]
        result = _execute(w, proposal, gw)
        w.store.get_proposal = original  # type: ignore[method-assign]
        assert gw.submit_calls == 0
        assert result.blocked is True
        assert w.store.list_execution_orders(proposal_id="s1") == ()
        w.close()

    def test_a_flip_after_the_intent_immediately_before_submit_submits_nothing(self, tmp_path):
        """C: the last-moment re-read must stop the order after the intent exists."""
        w = world(tmp_path)
        proposal = sell("s1", "10")
        reserve(w, proposal)
        gw = w.mutate()
        real = w.store.kill_switch_active
        # the intent transaction reads the switch three times (twice directly, once
        # via build_policy_context); the fourth read is the last-moment check inside
        # _submit_leg, and that is the one this case flips
        seen: list[str] = []

        def staged(*a, **kw):
            import traceback

            caller = traceback.extract_stack()[-2].name
            seen.append(caller)
            return caller == "_submit_leg"

        w.store.kill_switch_active = staged  # type: ignore[method-assign]
        result = _execute(w, proposal, gw)
        w.store.kill_switch_active = real  # type: ignore[method-assign]
        assert seen[-1] == "_submit_leg", (
            "there must be a kill-switch read inside _submit_leg, after the intent "
            f"transaction; reads seen: {seen}"
        )
        assert gw.submit_calls == 0, "the last-moment check must stop the order"
        record = w.store.get_execution_order(proposal.legs[0].client_order_id)
        assert record is not None, "the submission intent must have been persisted first"
        assert record.state is ExecutionState.NOT_SUBMITTED
        assert record.broker_order_id is None
        assert result.blocked is True
        assert result.submitted is False
        assert "kill switch" in result.block_reason
        kinds = [e.event_type for e in w.store.list_audit(proposal_id="s1")]
        assert AuditEventType.ORDER_NOT_SUBMITTED in kinds
        assert AuditEventType.EXECUTION_BLOCKED in kinds
        assert active_sell_qty(w.store) == Decimal("0"), "a proven abort releases capacity"
        w.close()

    def test_a_flip_after_the_broker_call_does_not_pretend_nothing_was_sent(self, tmp_path):
        """D: the order really went out. It must be acknowledged, never NOT_SUBMITTED."""
        w = world(tmp_path)
        proposal = sell("s1", "10")
        reserve(w, proposal)
        gw = w.mutate()
        original = gw.submit_order

        def flip_after_submit(request):
            payload = original(request)
            w.store.set_kill_switch(True, now=DEFAULT_NOW)
            return payload

        gw.submit_order = flip_after_submit  # type: ignore[method-assign]
        result = _execute(w, proposal, gw)
        record = w.store.get_execution_order(proposal.legs[0].client_order_id)
        w.close()
        assert gw.submit_calls == 1
        assert record.state is not ExecutionState.NOT_SUBMITTED, (
            "an order that reached the broker must never be recorded as unsent"
        )
        assert record.state in {ExecutionState.FILLED, ExecutionState.SUBMITTED,
                                ExecutionState.PARTIALLY_FILLED,
                                ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION}
        assert result.submitted is True

    def test_a_flip_after_the_broker_call_is_lost_still_recovers_as_unknown(self, tmp_path):
        w = world(tmp_path)
        proposal = sell("s1", "10")
        reserve(w, proposal)
        gw = w.mutate(timeout_after_accept=True)
        original = gw.submit_order

        def flip_then_timeout(request):
            try:
                return original(request)
            finally:
                w.store.set_kill_switch(True, now=DEFAULT_NOW)

        gw.submit_order = flip_then_timeout  # type: ignore[method-assign]
        _execute(w, proposal, gw)
        record = w.store.get_execution_order(proposal.legs[0].client_order_id)
        assert record.state is ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
        assert active_sell_qty(w.store) == Decimal("10")
        w.close()

    def test_the_kill_switch_is_read_again_between_legs(self, tmp_path):
        """A multi-leg proposal must stop mid-flight, not finish the batch."""
        w, proposal = _three_leg_world(tmp_path)
        gw = w.mutate()
        original = gw.submit_order

        def flip_after_first(request):
            payload = original(request)
            w.store.set_kill_switch(True, now=DEFAULT_NOW)
            return payload

        gw.submit_order = flip_after_first  # type: ignore[method-assign]
        _execute(w, proposal, gw)
        rows = {r.leg_index: r for r in w.store.list_execution_orders(proposal_id="m1")}
        w.close()
        assert gw.submit_calls == 1, "legs 1 and 2 must not go out after the flip"
        assert rows[1].state is ExecutionState.NOT_SUBMITTED
        assert rows[2].state is ExecutionState.NOT_SUBMITTED


# =====================================================  T+1 LIQUIDITY / SETTLEMENT


class TestSettlementAndLiquidity:
    def test_sell_proceeds_are_not_investable_until_t_plus_one(self, tmp_path):
        from opaca.treasury.liquidity import compute_liquidity

        w = world(tmp_path, qty="100", cash="100000")
        proposal = sell("s1", "10")
        reserve(w, proposal)
        result = _execute(w, proposal)
        assert result.state is ExecutionState.FILLED
        proceeds = Decimal("10") * SGOV
        events = w.store.load_settlement_events()
        assert len(events) == 1
        event = events[0]
        assert event.amount == proceeds
        assert event.settlement_date > DEFAULT_NOW.date(), "T+1, not same-day"

        snapshot = w.store.latest_snapshot()
        cash_now = snapshot.broker.cash if snapshot else Decimal("0")
        recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
        broker_cash = recon.snapshot.broker.cash
        assert broker_cash == Decimal("100000") + proceeds, "paper credits instantly"

        scenario = w.store.get_scenario()
        today = compute_liquidity(
            recon.snapshot.broker, w.store.load_obligations(),
            w.store.load_settlement_events(), scenario.operating_reserve,
            DEFAULT_NOW.date(),
        )
        assert today.settled_cash == Decimal("100000"), (
            "the instantly-credited proceeds must be excluded until they settle"
        )
        assert today.unsettled_total == proceeds

        settled_day = compute_liquidity(
            recon.snapshot.broker, w.store.load_obligations(),
            w.store.load_settlement_events(), scenario.operating_reserve,
            event.settlement_date,
        )
        assert settled_day.settled_cash == Decimal("100000") + proceeds
        assert settled_day.unsettled_total == Decimal("0")
        del cash_now
        w.close()

    def test_unsettled_proceeds_cannot_fund_a_buy_today(self, tmp_path):
        """The economic form of the same rule."""
        from opaca.treasury.liquidity import compute_liquidity

        w = world(tmp_path, qty="100", cash="100000")
        proposal = sell("s1", "60")
        reserve(w, proposal)
        _execute(w, proposal)
        recon = reconcile(w.store, w.read(), now=DEFAULT_NOW)
        scenario = w.store.get_scenario()
        today = compute_liquidity(
            recon.snapshot.broker, w.store.load_obligations(),
            w.store.load_settlement_events(), scenario.operating_reserve,
            DEFAULT_NOW.date(),
        )
        later = compute_liquidity(
            recon.snapshot.broker, w.store.load_obligations(),
            w.store.load_settlement_events(), scenario.operating_reserve,
            w.store.load_settlement_events()[0].settlement_date,
        )
        assert later.investable_cash > today.investable_cash, (
            "settlement must strictly increase deployable cash"
        )
        assert later.investable_cash - today.investable_cash == Decimal("60") * SGOV
        w.close()

    def test_settlement_events_are_idempotent_across_recovery(self, tmp_path):
        w = world(tmp_path, qty="100")
        proposal = sell("s1", "10")
        reserve(w, proposal)
        _execute(w, proposal, partial_fill_qty=Decimal("4"))
        assert len(w.store.load_settlement_events()) == 1
        for _ in range(5):
            recover_proposal(w.store, w.read(), "s1", now=DEFAULT_NOW)
        assert len(w.store.load_settlement_events()) == 1
        w.close()

    def test_progressive_fills_create_one_event_per_increment_only(self, tmp_path):
        w = world(tmp_path, qty="100")
        proposal = sell("s1", "30")
        reserve(w, proposal)
        gw = w.mutate(partial_fill_qty=Decimal("10"))
        _execute(w, proposal, gw)
        coid = proposal.legs[0].client_order_id
        totals = []
        for filled, qty in (("20", "80"), ("30", "70")):
            w.orders[coid]["filled_qty"] = filled
            w.orders[coid]["status"] = "filled" if filled == "30" else "partially_filled"
            w.positions[0]["qty"] = qty
            w.positions[0]["qty_available"] = qty
            recover_proposal(w.store, w.read(), "s1", now=DEFAULT_NOW)
            totals.append(sum((e.amount for e in w.store.load_settlement_events()), Decimal("0")))
        assert totals[-1] == Decimal("30") * SGOV, "proceeds total the filled notional exactly"
        assert len(w.store.load_settlement_events()) == 3
        w.close()

    def test_a_buy_creates_no_settlement_proceeds(self, tmp_path):
        w = world(tmp_path, qty="0", cash="100000")
        proposal = buy("b1", "10")
        reserve(w, proposal)
        _execute(w, proposal)
        assert w.store.load_settlement_events() == ()
        w.close()
