"""RED-phase contracts for persisted Wheel approval bindings."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from opaca.wheel.authority import approval_is_current, approval_matches
from opaca.wheel.models import WheelAction, WheelApprovalBinding
from opaca.wheel.store import WheelStore

NOW = datetime(2026, 9, 3, 14, 0, tzinfo=UTC)


def approval() -> WheelApprovalBinding:
    return WheelApprovalBinding(
        wheel_decision_run_id="run-2026-09-03-001",
        attempt_number=1,
        occ_symbol="SPY260903P00746000",
        action=WheelAction.SELL_CASH_SECURED_PUT,
        contracts=1,
        assignment_capital=Decimal("74600"),
        approved_sell_limit_premium=Decimal("1.00"),
        approved_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )


def new_store(tmp_path: Path) -> WheelStore:
    return WheelStore(tmp_path / "wheel-approval.sqlite3")


def test_approval_persists_loads_and_expires_at_exactly_five_minutes(
    tmp_path: Path,
) -> None:
    binding = approval()
    with new_store(tmp_path) as store:
        store.persist_approval(binding)
        loaded = store.load_approval(binding.wheel_decision_run_id)

    assert loaded == binding
    assert approval_is_current(loaded, NOW + timedelta(minutes=4, seconds=59))
    assert not approval_is_current(loaded, NOW + timedelta(minutes=5))
    assert not approval_is_current(loaded, NOW - timedelta(seconds=1))


def test_approval_binds_every_amendment_field() -> None:
    binding = approval()

    assert approval_matches(binding, binding)
    assert not approval_matches(
        binding,
        replace(binding, wheel_decision_run_id="run-other"),
    )
    assert not approval_matches(binding, replace(binding, attempt_number=2))
    assert not approval_matches(binding, replace(binding, occ_symbol="QQQ260903P00600000"))
    assert not approval_matches(binding, replace(binding, action=WheelAction.HOLD))
    assert not approval_matches(binding, replace(binding, contracts=2))
    assert not approval_matches(
        binding,
        replace(binding, assignment_capital=Decimal("60000")),
    )
    assert not approval_matches(
        binding,
        replace(binding, approved_sell_limit_premium=Decimal("0.99")),
    )

