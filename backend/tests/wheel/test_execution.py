"""RED-phase contracts for the paper-only Wheel CSP execution gate."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from opaca.domain.models import AuthorityDecision, AuthorityResult, BrokerEnvironment
from opaca.wheel.authority import WheelAuthorityContext, decide_wheel_authority
from opaca.wheel.execution import (
    ExecutionStatus,
    OptionOrderRequest,
    WheelExecutionAuthorization,
    submit_authorized_csp,
)
from opaca.wheel.lifecycle import AuthorizedWheelOrder
from opaca.wheel.models import OptionContract, OptionQuote
from opaca.wheel.policy import WheelGuardEngine
from opaca.wheel.reconciliation import WheelBrokerOrder
from opaca.wheel.store import WheelStore
from tests.wheel.test_atomic_reservation import (
    NOW,
    authorize,
    new_store,
    policy_context,
    proposal,
)


class FakeExecutionGateway:
    def __init__(self, response: object | None = None, error: Exception | None = None) -> None:
        self.calls: list[OptionOrderRequest] = []
        self.response = response
        self.error = error

    def submit_order(self, request: OptionOrderRequest) -> object:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def build_authorization(
    store: WheelStore,
    run_id: str = "execution",
) -> tuple[WheelExecutionAuthorization, object, AuthorityDecision]:
    candidate = proposal(run_id)
    order = authorize(store, run_id)
    checks = WheelGuardEngine().evaluate(
        replace(policy_context(store), reservations=()),
        candidate,
    )
    authority = decide_wheel_authority(
        WheelAuthorityContext(
            risk_capital_base=Decimal("100000"),
            proposed_assignment_capital=Decimal("10000"),
            post_trade_underlying_exposure=Decimal("10000"),
            post_trade_aggregate_exposure=Decimal("10000"),
            policy_decision=checks,
        )
    )
    return (
        WheelExecutionAuthorization(
            order=order,
            wheel_decision_run_id=run_id,
            attempt_number=1,
            proposal=candidate,
            assignment_capital=Decimal("10000"),
        ),
        replace(policy_context(store), reservations=()),
        authority,
    )


def submit(
    store: WheelStore,
    gateway: FakeExecutionGateway,
    authorization: WheelExecutionAuthorization,
    context: object,
    authority: AuthorityDecision,
    *,
    expected_snapshot_version: str = "snapshot-1",
    contract: OptionContract | None = None,
    quote: OptionQuote | None = None,
    approval: object | None = None,
    account_id: str = "competition-paper",
) -> object:
    current_contract = authorization.proposal.contract if contract is None else contract
    current_quote = authorization.proposal.quote if quote is None else quote
    return submit_authorized_csp(
        store,
        gateway,
        account_id=account_id,
        expected_snapshot_version=expected_snapshot_version,
        authorization=authorization,
        policy_context=context,
        authority=authority,
        authoritative_contract=current_contract,
        authoritative_quote=current_quote,
        approval=approval,
        now=NOW,
    )


def valid_response(authorization: WheelExecutionAuthorization) -> WheelBrokerOrder:
    return WheelBrokerOrder(
        client_order_id=authorization.order.client_order_id,
        occ_symbol=authorization.proposal.contract.occ_symbol,
        side="SELL",
        right=authorization.proposal.contract.right,
        status="FILLED",
        contracts=1,
        filled_contracts=1,
    )


def test_all_final_gates_pass_and_submit_once(tmp_path: Path) -> None:
    with new_store(tmp_path / "execution-success.sqlite3") as store:
        authorization, context, authority = build_authorization(store)
        gateway = FakeExecutionGateway(response=valid_response(authorization))

        result = submit(store, gateway, authorization, context, authority)

        assert result.status is ExecutionStatus.SUBMITTED
        assert len(gateway.calls) == 1
        request = gateway.calls[0]
        assert request.symbol == authorization.proposal.contract.occ_symbol
        assert request.contracts == 1
        assert request.limit_premium == Decimal("1")
        assert request.client_order_id == authorization.order.client_order_id
        assert request.time_in_force == "DAY"
        assert request.side == "SELL"


@pytest.mark.parametrize(
    "label,context_change",
    [
        ("live environment", {"environment": BrokerEnvironment.LIVE}),
        ("unverified environment", {"environment_verified": False}),
        ("kill switch", {"kill_switch_active": True}),
        ("account binding context", {"account_binding_matches": False}),
    ],
)
def test_environment_kill_switch_and_account_gates_block_submit(
    tmp_path: Path,
    label: str,
    context_change: dict[str, object],
) -> None:
    del label
    with new_store(tmp_path / "environment-gate.sqlite3") as store:
        authorization, context, authority = build_authorization(store)
        gateway = FakeExecutionGateway(response=valid_response(authorization))
        changed_context = replace(context, **context_change)  # type: ignore[arg-type]

        result = submit(store, gateway, authorization, changed_context, authority)

        assert result.status is ExecutionStatus.NOT_SUBMITTED
        assert gateway.calls == []


@pytest.mark.parametrize(
    "label,quote",
    [
        (
            "stale quote",
            OptionQuote(bid=Decimal("1"), ask=Decimal("1.05"), as_of=NOW - timedelta(seconds=16)),
        ),
        (
            "future quote",
            OptionQuote(bid=Decimal("1"), ask=Decimal("1.05"), as_of=NOW + timedelta(seconds=1)),
        ),
        (
            "bid below persisted limit",
            OptionQuote(bid=Decimal("0.99"), ask=Decimal("1.05"), as_of=NOW),
        ),
    ],
)
def test_final_quote_gate_blocks_stale_future_or_worse_price(
    tmp_path: Path,
    label: str,
    quote: OptionQuote,
) -> None:
    del label
    with new_store(tmp_path / "quote-gate.sqlite3") as store:
        authorization, context, authority = build_authorization(store)
        gateway = FakeExecutionGateway(response=valid_response(authorization))

        result = submit(
            store,
            gateway,
            authorization,
            context,
            authority,
            quote=quote,
        )

        assert result.status is ExecutionStatus.NOT_SUBMITTED
        assert gateway.calls == []


@pytest.mark.parametrize("field", ["occ_symbol", "multiplier"])
def test_changed_contract_identity_or_multiplier_blocks_submit(
    tmp_path: Path,
    field: str,
) -> None:
    with new_store(tmp_path / f"contract-{field}.sqlite3") as store:
        authorization, context, authority = build_authorization(store)
        gateway = FakeExecutionGateway(response=valid_response(authorization))
        changed = replace(
            authorization.proposal.contract,
            **{field: "OTHER260904P00100000" if field == "occ_symbol" else Decimal("101")},
        )

        result = submit(
            store,
            gateway,
            authorization,
            context,
            authority,
            contract=changed,
        )

        assert result.status is ExecutionStatus.NOT_SUBMITTED
        assert gateway.calls == []


def test_snapshot_mismatch_and_missing_local_order_block_submit(tmp_path: Path) -> None:
    with new_store(tmp_path / "snapshot-gate.sqlite3") as store:
        authorization, context, authority = build_authorization(store)
        gateway = FakeExecutionGateway(response=valid_response(authorization))
        stale = submit(
            store,
            gateway,
            authorization,
            context,
            authority,
            expected_snapshot_version="snapshot-2",
        )
        assert stale.status is ExecutionStatus.NOT_SUBMITTED
        assert gateway.calls == []

        missing = replace(
            authorization,
            order=AuthorizedWheelOrder(
                client_order_id="wheel-missing",
                reservation_id="reservation-missing",
                assignment_capital=Decimal("10000"),
                state=authorization.order.state,
            ),
        )
        missing_result = submit(store, gateway, missing, context, authority)
        assert missing_result.status is ExecutionStatus.NOT_SUBMITTED
        assert gateway.calls == []


def test_hard_reject_cannot_reach_gateway(tmp_path: Path) -> None:
    with new_store(tmp_path / "reject-gate.sqlite3") as store:
        authorization, context, authority = build_authorization(store)
        rejected = AuthorityDecision(
            result=AuthorityResult.REJECT,
            reasons=("hard policy failure",),
            policy_decision=authority.policy_decision,
        )
        gateway = FakeExecutionGateway(response=valid_response(authorization))

        result = submit(store, gateway, authorization, context, rejected)

        assert result.status is ExecutionStatus.NOT_SUBMITTED
        assert gateway.calls == []


def test_approval_required_accepts_exact_current_binding(tmp_path: Path) -> None:
    from opaca.wheel.models import WheelApprovalBinding

    with new_store(tmp_path / "approval-success.sqlite3") as store:
        authorization, context, auto = build_authorization(store)
        approval = WheelApprovalBinding(
            wheel_decision_run_id=authorization.wheel_decision_run_id,
            attempt_number=authorization.attempt_number,
            occ_symbol=authorization.proposal.contract.occ_symbol,
            action=authorization.proposal.action,
            contracts=1,
            assignment_capital=authorization.assignment_capital,
            approved_sell_limit_premium=authorization.proposal.sell_limit_premium,
            approved_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=4),
        )
        approval_authority = replace(auto, result=AuthorityResult.APPROVAL_REQUIRED)
        gateway = FakeExecutionGateway(response=valid_response(authorization))

        result = submit(
            store,
            gateway,
            authorization,
            context,
            approval_authority,
            approval=approval,
        )

        assert result.status is ExecutionStatus.SUBMITTED
        assert len(gateway.calls) == 1


def test_expired_or_changed_approval_cannot_submit(tmp_path: Path) -> None:
    from opaca.wheel.models import WheelApprovalBinding

    with new_store(tmp_path / "approval-stale.sqlite3") as store:
        authorization, context, auto = build_authorization(store)
        approval = WheelApprovalBinding(
            wheel_decision_run_id=authorization.wheel_decision_run_id,
            attempt_number=authorization.attempt_number,
            occ_symbol=authorization.proposal.contract.occ_symbol,
            action=authorization.proposal.action,
            contracts=1,
            assignment_capital=authorization.assignment_capital,
            approved_sell_limit_premium=authorization.proposal.sell_limit_premium,
            approved_at=NOW - timedelta(minutes=5),
            expires_at=NOW,
        )
        approval_authority = replace(auto, result=AuthorityResult.APPROVAL_REQUIRED)
        gateway = FakeExecutionGateway(response=valid_response(authorization))

        expired = submit(
            store,
            gateway,
            authorization,
            context,
            approval_authority,
            approval=approval,
        )
        assert expired.status is ExecutionStatus.NOT_SUBMITTED
        assert gateway.calls == []

        changed = replace(approval, occ_symbol="QQQ260904P00100000")
        mismatched = submit(
            store,
            gateway,
            authorization,
            context,
            approval_authority,
            approval=changed,
        )
        assert mismatched.status is ExecutionStatus.NOT_SUBMITTED
        assert gateway.calls == []


def test_exception_after_submit_is_unknown_and_never_retried(tmp_path: Path) -> None:
    with new_store(tmp_path / "execution-unknown.sqlite3") as store:
        authorization, context, authority = build_authorization(store)
        gateway = FakeExecutionGateway(error=TimeoutError("outcome unknown"))

        result = submit(store, gateway, authorization, context, authority)

        assert result.status is ExecutionStatus.UNKNOWN
        assert len(gateway.calls) == 1
        assert len(store.active_assignment_reservations()) == 1
        assert store._conn.execute(
            "SELECT status FROM wheel_orders WHERE client_order_id = ?",
            (authorization.order.client_order_id,),
        ).fetchone()[0] == "UNKNOWN"


def test_mismatched_submit_response_is_unknown_not_reconciled(tmp_path: Path) -> None:
    with new_store(tmp_path / "response-mismatch.sqlite3") as store:
        authorization, context, authority = build_authorization(store)
        response = replace(
            valid_response(authorization),
            occ_symbol="QQQ260904P00100000",
        )
        gateway = FakeExecutionGateway(response=response)

        result = submit(store, gateway, authorization, context, authority)

        assert result.status is ExecutionStatus.UNKNOWN
        assert len(gateway.calls) == 1
        assert len(store.active_assignment_reservations()) == 1
