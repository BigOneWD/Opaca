"""RED-phase contracts for hard Wheel policy checks."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from opaca.domain.models import (
    BrokerEnvironment,
    CheckId,
    PolicyCheckResult,
    PolicyDecision,
)
from opaca.wheel.config import WheelPolicy
from opaca.wheel.models import (
    OptionContract,
    OptionQuote,
    OptionRight,
    WheelAction,
    WheelState,
)
from opaca.wheel.policy import WheelGuardEngine, WheelPolicyContext, WheelProposal
from opaca.wheel.store import WheelReservation

NOW = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)
CONTRACT = OptionContract(
    occ_symbol="SPY260903P00746000",
    underlying="SPY",
    right=OptionRight.PUT,
    strike=Decimal("746"),
    expiration=date(2026, 9, 3),
    multiplier=Decimal("100"),
    active=True,
    tradable=True,
)
QUOTE = OptionQuote(bid=Decimal("1.00"), ask=Decimal("1.05"), as_of=NOW)
POLICY = WheelPolicy()


def proposal(
    *,
    contract: OptionContract = CONTRACT,
    quote: OptionQuote = QUOTE,
    contracts: int = 1,
    limit: Decimal | None = None,
) -> WheelProposal:
    return WheelProposal(
        action=WheelAction.SELL_CASH_SECURED_PUT,
        contract=contract,
        quote=quote,
        contracts=contracts,
        sell_limit_premium=quote.bid if limit is None else limit,
    )


def context(**overrides: object) -> WheelPolicyContext:
    values: dict[str, object] = {
        "risk_capital_base": Decimal("100000"),
        "reconciled_cash": Decimal("100000"),
        "held_share_exposure": {},
        "reservations": (),
        "permitted_underlyings": frozenset({"SPY", "QQQ"}),
        "wheel_state": WheelState.CASH,
        "unresolved_underlyings": frozenset(),
        "options_buying_power": Decimal("100000"),
        "broker_collateral_consistent": True,
        "account_binding_matches": True,
        "environment": BrokerEnvironment.PAPER,
        "environment_verified": True,
        "kill_switch_active": False,
        "now": NOW,
        "policy": POLICY,
    }
    values.update(overrides)
    return WheelPolicyContext(**values)  # type: ignore[arg-type]


def check(decision: PolicyDecision, check_id: CheckId) -> PolicyCheckResult:
    return decision.result_for(check_id)


def evaluate(
    candidate: WheelProposal | None = None,
    **overrides: object,
) -> PolicyDecision:
    return WheelGuardEngine().evaluate(
        context(**overrides), candidate if candidate is not None else proposal()
    )


def test_check_ids_seventeen_through_twenty_two_are_reserved_for_wheel() -> None:
    assert CheckId.CHECK_17.value == "CHECK-17"
    assert CheckId.CHECK_18.value == "CHECK-18"
    assert CheckId.CHECK_19.value == "CHECK-19"
    assert CheckId.CHECK_20.value == "CHECK-20"
    assert CheckId.CHECK_21.value == "CHECK-21"
    assert CheckId.CHECK_22.value == "CHECK-22"


def test_valid_csp_passes_all_six_wheel_checks() -> None:
    small_contract = replace(CONTRACT, strike=Decimal("100"))
    decision = evaluate(proposal(contract=small_contract))

    assert decision.passed is True
    assert {result.check_id for result in decision.results} == {
        CheckId.CHECK_17,
        CheckId.CHECK_18,
        CheckId.CHECK_19,
        CheckId.CHECK_20,
        CheckId.CHECK_21,
        CheckId.CHECK_22,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"wheel_state": WheelState.UNKNOWN},
        {"unresolved_underlyings": frozenset({"SPY"})},
        {"kill_switch_active": True},
        {"environment": BrokerEnvironment.LIVE},
        {"environment_verified": False},
    ],
)
def test_check_17_blocks_unknown_unresolved_or_unsafe_runtime_state(
    overrides: dict[str, object],
) -> None:
    decision = evaluate(**overrides)  # type: ignore[arg-type]

    assert decision.passed is False
    assert check(decision, CheckId.CHECK_17).passed is False


def test_check_18_enforces_whitelist_tradability_and_dte_window() -> None:
    not_allowed = replace(CONTRACT, underlying="IWM")
    inactive = replace(CONTRACT, active=False)
    too_late = replace(CONTRACT, expiration=date(2026, 9, 10))

    for candidate in (not_allowed, inactive, too_late):
        result = check(evaluate(proposal(contract=candidate)), CheckId.CHECK_18)
        assert result.passed is False


def test_check_19_rejects_future_stale_bad_quote_and_changed_limit() -> None:
    cases = (
        OptionQuote(bid=Decimal("1"), ask=Decimal("1"), as_of=NOW + timedelta(seconds=1)),
        OptionQuote(bid=Decimal("1"), ask=Decimal("1"), as_of=NOW - timedelta(seconds=16)),
        OptionQuote(bid=Decimal("0"), ask=Decimal("1"), as_of=NOW),
        OptionQuote(bid=Decimal("2"), ask=Decimal("1"), as_of=NOW),
    )
    for candidate_quote in cases:
        result = check(
            evaluate(proposal(quote=candidate_quote, limit=candidate_quote.bid)),
            CheckId.CHECK_19,
        )
        assert result.passed is False

    changed_limit = check(
        evaluate(proposal(limit=Decimal("0.99"))),
        CheckId.CHECK_19,
    )
    assert changed_limit.passed is False


def test_quote_age_exactly_fifteen_seconds_is_valid() -> None:
    candidate_quote = OptionQuote(
        bid=Decimal("1"), ask=Decimal("1"), as_of=NOW - timedelta(seconds=15)
    )

    result = check(
        evaluate(proposal(quote=candidate_quote)),
        CheckId.CHECK_19,
    )

    assert result.passed is True


def test_check_20_uses_immutable_risk_capital_and_keeps_assigned_share_exposure() -> None:
    decision = evaluate(
        proposal(),
        risk_capital_base=Decimal("100000"),
        reconciled_cash=Decimal("200000"),
        held_share_exposure={"SPY": Decimal("25000")},
        options_buying_power=Decimal("400000"),
    )

    result = check(decision, CheckId.CHECK_20)
    assert result.passed is False
    assert "25000" in result.detail


def test_check_20_counts_active_reservations_and_available_cash() -> None:
    reservations = (
        WheelReservation("existing", "SPY", Decimal("10000"), "ACTIVE"),
    )
    decision = evaluate(reservations=reservations)

    result = check(decision, CheckId.CHECK_20)
    assert result.passed is False
    assert "available" in result.detail.lower() or "exposure" in result.detail.lower()


def test_check_21_rejects_missing_or_contradictory_option_collateral() -> None:
    for overrides in (
        {"options_buying_power": None},
        {"options_buying_power": Decimal("1")},
        {"broker_collateral_consistent": False},
    ):
        result = check(evaluate(**overrides), CheckId.CHECK_21)
        assert result.passed is False


def test_buying_power_diagnostic_cannot_enlarge_internal_authority() -> None:
    small_contract = replace(CONTRACT, strike=Decimal("100"))
    decision = evaluate(
        proposal(contract=small_contract),
        options_buying_power=Decimal("400000"),
        reconciled_cash=Decimal("100000"),
        risk_capital_base=Decimal("100000"),
    )
    assert check(decision, CheckId.CHECK_20).passed is True


def test_check_22_fails_account_binding_mismatch() -> None:
    result = check(
        evaluate(account_binding_matches=False),
        CheckId.CHECK_22,
    )

    assert result.passed is False
