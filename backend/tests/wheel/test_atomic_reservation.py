"""RED-phase contracts for atomic Wheel authorization and reservation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from opaca.domain.models import AuthorityResult, BrokerEnvironment
from opaca.wheel.authority import WheelAuthorityContext, decide_wheel_authority
from opaca.wheel.config import WheelPolicy
from opaca.wheel.lifecycle import (
    AuthorizedWheelOrder,
    WheelStaleSnapshotError,
    authorize_and_reserve,
)
from opaca.wheel.models import (
    OptionContract,
    OptionQuote,
    OptionRight,
    WheelAction,
    WheelState,
)
from opaca.wheel.policy import WheelGuardEngine, WheelPolicyContext, WheelProposal
from opaca.wheel.store import WheelAccountMismatchError, WheelStore

NOW = datetime(2026, 9, 3, 14, 0, tzinfo=UTC)


def proposal(run_id: str) -> WheelProposal:
    return WheelProposal(
        action=WheelAction.SELL_CASH_SECURED_PUT,
        contract=OptionContract(
            occ_symbol=f"SPY260904P00100000-{run_id}",
            underlying="SPY",
            right=OptionRight.PUT,
            strike=Decimal("100"),
            expiration=date(2026, 9, 4),
            multiplier=Decimal("100"),
            active=True,
            tradable=True,
        ),
        quote=OptionQuote(bid=Decimal("1"), ask=Decimal("1.05"), as_of=NOW),
        contracts=1,
        sell_limit_premium=Decimal("1"),
    )


def policy_context(store: WheelStore) -> WheelPolicyContext:
    return WheelPolicyContext(
        risk_capital_base=Decimal("100000"),
        reconciled_cash=Decimal("10000"),
        held_share_exposure={},
        reservations=tuple(store.active_assignment_reservations()),
        permitted_underlyings=frozenset({"SPY"}),
        wheel_state=WheelState.CASH,
        unresolved_underlyings=frozenset(),
        options_buying_power=Decimal("10000"),
        broker_collateral_consistent=True,
        account_binding_matches=True,
        environment=BrokerEnvironment.PAPER,
        environment_verified=True,
        kill_switch_active=False,
        now=NOW,
        policy=WheelPolicy(),
    )


def authorize(store: WheelStore, run_id: str) -> AuthorizedWheelOrder:
    candidate = proposal(run_id)
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
    assert authority.result is AuthorityResult.AUTO
    return authorize_and_reserve(
        store,
        account_id="competition-paper",
        expected_snapshot_version="snapshot-1",
        proposal=candidate,
        policy_context=policy_context(store),
        authority=authority,
        wheel_decision_run_id=run_id,
        attempt_number=1,
        now=NOW,
    )


def new_store(path: Path) -> WheelStore:
    store = WheelStore(path)
    store.bootstrap_account("competition-paper", Decimal("100000"), NOW)
    store.set_snapshot_version("snapshot-1")
    return store


def test_two_writer_connections_cannot_double_spend_unreserved_cash(tmp_path: Path) -> None:
    path = tmp_path / "wheel-atomic.sqlite3"
    with new_store(path) as initial:
        initial.close()

    def call(run_id: str) -> AuthorizedWheelOrder | Exception:
        store = WheelStore(path)
        try:
            return authorize(store, run_id)
        except Exception as exc:  # pragma: no cover - assertion classifies it below
            return exc
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(call, ("run-a", "run-b")))

    assert sum(isinstance(outcome, AuthorizedWheelOrder) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, Exception) for outcome in outcomes) == 1
    with WheelStore(path) as store:
        active = store.active_assignment_reservations()
        assert len(active) == 1
        assert active[0].amount == Decimal("10000")


def test_stale_snapshot_is_rejected_before_reservation(tmp_path: Path) -> None:
    path = tmp_path / "wheel-stale.sqlite3"
    with new_store(path) as store:
        store.set_snapshot_version("snapshot-2")
        with pytest.raises(WheelStaleSnapshotError):
            authorize(store, "stale-run")
        assert store.active_assignment_reservations() == []


def test_account_binding_mismatch_is_rejected_before_reservation(tmp_path: Path) -> None:
    path = tmp_path / "wheel-account.sqlite3"
    with new_store(path) as store:
        candidate = proposal("wrong-account")
        checks = WheelGuardEngine().evaluate(policy_context(store), candidate)
        authority = decide_wheel_authority(
            WheelAuthorityContext(
                risk_capital_base=Decimal("100000"),
                proposed_assignment_capital=Decimal("10000"),
                post_trade_underlying_exposure=Decimal("10000"),
                post_trade_aggregate_exposure=Decimal("10000"),
                policy_decision=checks,
            )
        )
        with pytest.raises(WheelAccountMismatchError):
            authorize_and_reserve(
                store,
                account_id="another-paper-account",
                expected_snapshot_version="snapshot-1",
                proposal=candidate,
                policy_context=policy_context(store),
                authority=authority,
                wheel_decision_run_id="wrong-account",
                attempt_number=1,
                now=NOW,
            )
        assert store.active_assignment_reservations() == []
