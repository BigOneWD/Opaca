"""Deterministic hard policy checks for the V1 Competition Wheel."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from opaca.domain.models import (
    BrokerEnvironment,
    CheckId,
    PolicyCheckResult,
    PolicyDecision,
)
from opaca.wheel.config import WheelPolicy
from opaca.wheel.models import OptionContract, OptionQuote, OptionRight, WheelAction, WheelState
from opaca.wheel.store import WheelReservation


@dataclass(frozen=True)
class WheelProposal:
    """Selector output needed by hard Wheel policy."""

    action: WheelAction
    contract: OptionContract
    quote: OptionQuote
    contracts: int
    sell_limit_premium: Decimal


@dataclass(frozen=True)
class WheelPolicyContext:
    """Authoritative reconciled inputs for one Wheel policy evaluation."""

    risk_capital_base: Decimal
    reconciled_cash: Decimal
    held_share_exposure: Mapping[str, Decimal]
    reservations: Sequence[WheelReservation]
    permitted_underlyings: frozenset[str]
    wheel_state: WheelState
    unresolved_underlyings: frozenset[str]
    options_buying_power: Decimal | None
    broker_collateral_consistent: bool
    account_binding_matches: bool
    environment: BrokerEnvironment
    environment_verified: bool
    kill_switch_active: bool
    now: datetime
    policy: WheelPolicy
    session_close: datetime | None = None


def assignment_capital(proposal: WheelProposal) -> Decimal:
    """Return strike × authoritative multiplier × contract count."""
    return proposal.contract.strike * proposal.contract.multiplier * proposal.contracts


def _result(check_id: CheckId, passed: bool, detail: str) -> PolicyCheckResult:
    return PolicyCheckResult(check_id=check_id, passed=passed, hard=True, detail=detail)


def _check_17(context: WheelPolicyContext, proposal: WheelProposal) -> PolicyCheckResult:
    problems: list[str] = []
    if context.kill_switch_active:
        problems.append("kill switch active")
    if context.environment is not BrokerEnvironment.PAPER or not context.environment_verified:
        problems.append("PAPER environment is not verified")
    if context.wheel_state is not WheelState.CASH:
        problems.append(f"Wheel state is {context.wheel_state.value}")
    if proposal.contract.underlying in context.unresolved_underlyings:
        problems.append("underlying has unresolved Wheel state")
    return _result(
        CheckId.CHECK_17,
        not problems,
        "opening Wheel state and runtime safety gates pass"
        if not problems
        else "; ".join(problems),
    )


def _check_18(context: WheelPolicyContext, proposal: WheelProposal) -> PolicyCheckResult:
    contract = proposal.contract
    problems: list[str] = []
    if contract.underlying not in context.permitted_underlyings:
        problems.append("underlying is not whitelisted")
    if proposal.action is not WheelAction.SELL_CASH_SECURED_PUT:
        problems.append("action is not SELL_CASH_SECURED_PUT")
    if contract.right is not OptionRight.PUT:
        problems.append("contract is not a PUT")
    if not contract.active or not contract.tradable:
        problems.append("contract is not active and tradable")
    dte = (contract.expiration - context.now.date()).days
    if not context.policy.min_dte_days <= dte <= context.policy.max_dte_days:
        problems.append("contract DTE is outside the V1 window")
    if contract.multiplier <= 0:
        problems.append("contract multiplier is not positive")
    if proposal.contracts != context.policy.opening_contracts or not isinstance(
        proposal.contracts, int
    ):
        problems.append("V1 requires exactly one integer contract")
    if context.session_close is not None and context.now >= context.session_close - timedelta(
        minutes=context.policy.preclose_blackout_minutes
    ):
        problems.append("opening is inside the final pre-close blackout")
    return _result(
        CheckId.CHECK_18,
        not problems,
        "option contract identity and V1 eligibility pass"
        if not problems
        else "; ".join(problems),
    )


def _check_19(context: WheelPolicyContext, proposal: WheelProposal) -> PolicyCheckResult:
    quote = proposal.quote
    age = context.now - quote.as_of
    problems: list[str] = []
    if quote.as_of.tzinfo is None or quote.as_of.utcoffset() != UTC.utcoffset(None):
        problems.append("quote timestamp is not timezone-aware UTC")
    elif age < timedelta(0):
        problems.append("quote timestamp is in the future")
    elif age > timedelta(seconds=context.policy.max_quote_age_seconds):
        problems.append("quote is older than the freshness limit")
    if quote.bid <= 0:
        problems.append("quote bid must be positive")
    if quote.ask < quote.bid:
        problems.append("quote ask is below bid")
    if proposal.sell_limit_premium != quote.bid:
        problems.append("sell limit premium must equal the authoritative bid")
    capital = assignment_capital(proposal)
    premium = quote.bid * proposal.contract.multiplier * proposal.contracts
    if capital <= 0 or premium / capital < context.policy.min_premium_yield_on_assignment:
        problems.append("premium yield is below the V1 minimum")
    return _result(
        CheckId.CHECK_19,
        not problems,
        "fresh executable quote economics pass" if not problems else "; ".join(problems),
    )


def _check_20(context: WheelPolicyContext, proposal: WheelProposal) -> PolicyCheckResult:
    capital = assignment_capital(proposal)
    active_total = Decimal("0")
    active_by_underlying: dict[str, Decimal] = {}
    for reservation in context.reservations:
        if reservation.status != "ACTIVE" or reservation.kind != "CASH_DEPLOYMENT":
            continue
        active_total += reservation.amount
        active_by_underlying[reservation.underlying] = (
            active_by_underlying.get(reservation.underlying, Decimal("0"))
            + reservation.amount
        )
    available_cash = context.reconciled_cash - active_total
    held = context.held_share_exposure.get(proposal.contract.underlying, Decimal("0"))
    post_name = (
        held
        + active_by_underlying.get(proposal.contract.underlying, Decimal("0"))
        + capital
    )
    existing_aggregate = sum(context.held_share_exposure.values(), Decimal("0")) + active_total
    post_aggregate = existing_aggregate + capital
    problems: list[str] = []
    if post_name > context.risk_capital_base * context.policy.hard_per_underlying_fraction:
        problems.append(
            f"held share exposure {held}; post-trade underlying exposure {post_name} "
            "exceeds hard cap"
        )
    if post_aggregate > context.risk_capital_base:
        problems.append(f"post-trade aggregate exposure {post_aggregate} exceeds risk capital")
    if capital > available_cash:
        problems.append(f"assignment capital {capital} exceeds available cash {available_cash}")
    detail = (
        "Wheel exposure and available cash pass using immutable risk capital"
        if not problems
        else "; ".join(problems)
    )
    return _result(CheckId.CHECK_20, not problems, detail)


def _check_21(context: WheelPolicyContext, proposal: WheelProposal) -> PolicyCheckResult:
    capital = assignment_capital(proposal)
    problems: list[str] = []
    buying_power = context.options_buying_power
    if buying_power is None:
        problems.append("options collateral diagnostic is missing")
    elif buying_power < capital:
        problems.append("options collateral diagnostic contradicts the proposed trade")
    if not context.broker_collateral_consistent:
        problems.append("broker collateral diagnostic is contradictory")
    return _result(
        CheckId.CHECK_21,
        not problems,
        "broker collateral diagnostic does not contradict internal feasibility"
        if not problems
        else "; ".join(problems),
    )


def _check_22(context: WheelPolicyContext) -> PolicyCheckResult:
    return _result(
        CheckId.CHECK_22,
        context.account_binding_matches,
        "Competition account binding matches"
        if context.account_binding_matches
        else "Competition account binding mismatch",
    )


class WheelGuardEngine:
    """Stateless hard Wheel guard; authority is evaluated in a later task."""

    def evaluate(
        self,
        context: WheelPolicyContext,
        proposal: WheelProposal,
    ) -> PolicyDecision:
        results = (
            _check_17(context, proposal),
            _check_18(context, proposal),
            _check_19(context, proposal),
            _check_20(context, proposal),
            _check_21(context, proposal),
            _check_22(context),
        )
        return PolicyDecision(passed=all(result.passed for result in results), results=results)
