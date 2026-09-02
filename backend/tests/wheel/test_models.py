"""RED-phase contract tests for the Wheel domain boundary.

These tests intentionally import the future ``opaca.wheel`` package.  Task 2
stops after observing the collection failure caused by the missing production
package; no production implementation belongs in this phase.
"""

import inspect
from dataclasses import fields
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from opaca.wheel.config import WheelPolicy
from opaca.wheel.models import (
    OptionContract,
    OptionIntent,
    OptionPosition,
    OptionQuote,
    OptionRight,
    WheelAction,
    WheelApprovalBinding,
    WheelShareLot,
    WheelState,
)

NOW = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
OCC_SYMBOL = "SPY260909P00746000"


def _contract(**overrides: object) -> OptionContract:
    values: dict[str, object] = {
        "occ_symbol": OCC_SYMBOL,
        "underlying": "SPY",
        "right": OptionRight.PUT,
        "strike": Decimal("746.00"),
        "expiration": date(2026, 9, 9),
        "multiplier": Decimal("100"),
        "active": True,
        "tradable": True,
    }
    values.update(overrides)
    return OptionContract(**values)


def _intent(**overrides: object) -> OptionIntent:
    values: dict[str, object] = {
        "action": WheelAction.SELL_CASH_SECURED_PUT,
        "underlying": "SPY",
        "market_view": "Constructive but willing to own the underlying lower.",
        "thesis": "A short-dated cash-secured put fits the ownership preference.",
        "willing_to_own_at_or_below": Decimal("746.00"),
        "dte_preference": 5,
        "confidence": Decimal("0.75"),
    }
    values.update(overrides)
    return OptionIntent(**values)


def _position(**overrides: object) -> OptionPosition:
    values: dict[str, object] = {
        "occ_symbol": OCC_SYMBOL,
        "underlying": "SPY",
        "right": OptionRight.PUT,
        "contracts": 1,
        "side": "SHORT",
    }
    values.update(overrides)
    return OptionPosition(**values)


def _share_lot(**overrides: object) -> WheelShareLot:
    values: dict[str, object] = {
        "underlying": "SPY",
        "shares": 100,
        "assignment_basis": Decimal("746.00"),
        "market_value": Decimal("75000.00"),
    }
    values.update(overrides)
    return WheelShareLot(**values)


def _approval(**overrides: object) -> WheelApprovalBinding:
    values: dict[str, object] = {
        "wheel_decision_run_id": "run-2026-09-02-001",
        "attempt_number": 1,
        "occ_symbol": OCC_SYMBOL,
        "action": WheelAction.SELL_CASH_SECURED_PUT,
        "contracts": 1,
        "assignment_capital": Decimal("74600.00"),
        "approved_sell_limit_premium": Decimal("0.08"),
        "approved_at": NOW,
        "expires_at": NOW + timedelta(minutes=5),
    }
    values.update(overrides)
    return WheelApprovalBinding(**values)


def test_wheel_enums_expose_only_the_intended_v1_states() -> None:
    assert OptionRight.PUT.value == "PUT"
    assert WheelAction.SELL_CASH_SECURED_PUT.value == "SELL_CASH_SECURED_PUT"
    assert WheelAction.HOLD.value == "HOLD"
    assert {state.value for state in WheelState} == {
        "CASH",
        "SHORT_PUT_OPEN",
        "SHARES_HELD",
        "COVERED_CALL_OPEN",
        "UNKNOWN",
    }


def test_option_contract_preserves_authoritative_identity_and_metadata() -> None:
    contract = _contract()

    assert contract.occ_symbol == OCC_SYMBOL
    assert contract.underlying == "SPY"
    assert contract.right is OptionRight.PUT
    assert contract.strike == Decimal("746.00")
    assert contract.expiration == date(2026, 9, 9)
    assert contract.multiplier == Decimal("100")
    assert contract.active is True
    assert contract.tradable is True


@pytest.mark.parametrize("field", ["occ_symbol", "underlying"])
def test_option_contract_rejects_empty_identity(field: str) -> None:
    with pytest.raises(ValueError):
        _contract(**{field: ""})


@pytest.mark.parametrize("strike", [Decimal("0"), Decimal("-0.01")])
def test_option_contract_rejects_non_positive_strike(strike: Decimal) -> None:
    with pytest.raises(ValueError):
        _contract(strike=strike)


@pytest.mark.parametrize("multiplier", [Decimal("0"), Decimal("-1")])
def test_option_contract_rejects_non_positive_multiplier(multiplier: Decimal) -> None:
    with pytest.raises(ValueError):
        _contract(multiplier=multiplier)


@pytest.mark.parametrize(
    ("bid", "ask"),
    [
        (Decimal("-0.01"), Decimal("0.08")),
        (Decimal("0.07"), Decimal("-0.01")),
    ],
)
def test_option_quote_rejects_negative_prices(bid: Decimal, ask: Decimal) -> None:
    with pytest.raises(ValueError):
        OptionQuote(bid=bid, ask=ask, as_of=NOW)


def test_option_quote_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        OptionQuote(
            bid=Decimal("0.07"),
            ask=Decimal("0.08"),
            as_of=datetime(2026, 9, 2, 15, 0),
        )


def test_option_quote_accepts_timezone_aware_utc_and_structural_zero() -> None:
    quote = OptionQuote(bid=Decimal("0"), ask=Decimal("0"), as_of=NOW)

    assert quote.bid == Decimal("0")
    assert quote.ask == Decimal("0")
    assert quote.as_of == NOW


@pytest.mark.parametrize("underlying", ["", " "])
def test_option_intent_rejects_empty_underlying(underlying: str) -> None:
    with pytest.raises(ValueError):
        _intent(underlying=underlying)


@pytest.mark.parametrize("ownership_price", [Decimal("0"), Decimal("-1")])
def test_option_intent_rejects_non_positive_ownership_price(ownership_price: Decimal) -> None:
    with pytest.raises(ValueError):
        _intent(willing_to_own_at_or_below=ownership_price)


@pytest.mark.parametrize("confidence", [Decimal("-0.01"), Decimal("1.01")])
def test_option_intent_rejects_confidence_outside_unit_interval(confidence: Decimal) -> None:
    with pytest.raises(ValueError):
        _intent(confidence=confidence)


def test_option_intent_accepts_sell_put_and_hold() -> None:
    sell_put = _intent()
    hold = _intent(action=WheelAction.HOLD)

    assert sell_put.action is WheelAction.SELL_CASH_SECURED_PUT
    assert hold.action is WheelAction.HOLD


def test_option_intent_does_not_accept_authoritative_broker_or_policy_fields() -> None:
    forbidden = {
        "occ_symbol",
        "multiplier",
        "contracts",
        "limit_premium",
        "assignment_capital",
        "authority_result",
        "policy_limits",
    }
    parameters = set(inspect.signature(OptionIntent).parameters)

    assert forbidden.isdisjoint(parameters)


def test_option_position_represents_a_short_put_separately() -> None:
    position = _position()

    assert position.occ_symbol == OCC_SYMBOL
    assert position.underlying == "SPY"
    assert position.right is OptionRight.PUT
    assert position.contracts == 1
    assert position.side == "SHORT"


@pytest.mark.parametrize("field", ["occ_symbol", "underlying"])
def test_option_position_rejects_empty_identity(field: str) -> None:
    with pytest.raises(ValueError):
        _position(**{field: ""})


@pytest.mark.parametrize("contracts", [0, -1])
def test_option_position_rejects_non_positive_contract_count(contracts: int) -> None:
    with pytest.raises(ValueError):
        _position(contracts=contracts)


@pytest.mark.parametrize("shares", [0, -1])
def test_wheel_share_lot_rejects_non_positive_share_quantity(shares: int) -> None:
    with pytest.raises(ValueError):
        _share_lot(shares=shares)


def test_wheel_share_lot_rejects_negative_assignment_basis() -> None:
    with pytest.raises(ValueError):
        _share_lot(assignment_basis=Decimal("-0.01"))


def test_wheel_share_lot_rejects_negative_market_value() -> None:
    with pytest.raises(ValueError):
        _share_lot(market_value=Decimal("-0.01"))


def test_wheel_policy_exposes_exact_v1_defaults() -> None:
    policy = WheelPolicy()

    assert policy.min_dte_days == 1
    assert policy.max_dte_days == 7
    assert policy.max_quote_age_seconds == 15
    assert policy.preclose_blackout_minutes == 30
    assert policy.min_premium_yield_on_assignment == Decimal("0.001")
    assert policy.hard_per_underlying_fraction == Decimal("0.25")
    assert policy.auto_proposal_fraction == Decimal("0.10")
    assert policy.auto_underlying_fraction == Decimal("0.10")
    assert policy.auto_aggregate_fraction == Decimal("0.20")
    assert policy.approval_ttl_minutes == 5
    assert policy.opening_contracts == 1
    assert policy.max_agent_attempts_per_run == 2


def test_wheel_policy_exposes_no_duplicate_aliases_for_v1_constants() -> None:
    names = {field.name for field in fields(WheelPolicy)}

    assert {
        "min_dte_days",
        "max_dte_days",
        "max_quote_age_seconds",
        "preclose_blackout_minutes",
        "min_premium_yield_on_assignment",
        "hard_per_underlying_fraction",
        "auto_proposal_fraction",
        "auto_underlying_fraction",
        "auto_aggregate_fraction",
        "approval_ttl_minutes",
        "opening_contracts",
        "max_agent_attempts_per_run",
    } <= names
    assert not {"quote_freshness_seconds", "max_quote_age"} & names


def test_wheel_policy_rejects_invalid_dte_ordering() -> None:
    with pytest.raises(ValueError):
        WheelPolicy(min_dte_days=0)

    with pytest.raises(ValueError):
        WheelPolicy(min_dte_days=4, max_dte_days=3)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hard_per_underlying_fraction", Decimal("-0.01")),
        ("auto_proposal_fraction", Decimal("1.01")),
        ("auto_underlying_fraction", Decimal("-0.01")),
        ("auto_aggregate_fraction", Decimal("1.01")),
    ],
)
def test_wheel_policy_rejects_fractions_outside_valid_range(field: str, value: Decimal) -> None:
    with pytest.raises(ValueError):
        WheelPolicy(**{field: value})


def test_wheel_policy_rejects_non_v1_opening_contract_count() -> None:
    with pytest.raises(ValueError):
        WheelPolicy(opening_contracts=2)


def test_wheel_policy_rejects_non_v1_agent_attempt_budget() -> None:
    with pytest.raises(ValueError):
        WheelPolicy(max_agent_attempts_per_run=3)


def test_wheel_approval_binding_binds_exact_amendment_fields() -> None:
    expected = {
        "wheel_decision_run_id",
        "attempt_number",
        "occ_symbol",
        "action",
        "contracts",
        "assignment_capital",
        "approved_sell_limit_premium",
        "approved_at",
        "expires_at",
    }
    parameters = set(inspect.signature(WheelApprovalBinding).parameters)
    binding = _approval()

    assert parameters == expected
    assert binding.expires_at - binding.approved_at == timedelta(minutes=5)


@pytest.mark.parametrize("field", ["wheel_decision_run_id", "occ_symbol"])
def test_wheel_approval_binding_rejects_empty_identity(field: str) -> None:
    with pytest.raises(ValueError):
        _approval(**{field: ""})
