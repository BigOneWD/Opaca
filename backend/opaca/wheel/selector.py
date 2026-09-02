"""Deterministic selection of one executable cash-secured put."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from opaca.wheel.config import WheelPolicy
from opaca.wheel.models import OptionContract, OptionIntent, OptionQuote, WheelAction


class NoCspSelection(ValueError):
    """No contract satisfied the structural and quote eligibility rules."""


@dataclass(frozen=True)
class SelectedCsp:
    """The selector's fully determined, one-contract CSP economics."""

    contract: OptionContract
    quote: OptionQuote
    contracts: int
    limit_premium: Decimal
    time_in_force: str
    assignment_capital: Decimal


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(None):
        raise NoCspSelection(f"{name} must be timezone-aware UTC")


def _eligible(
    intent: OptionIntent,
    contract: OptionContract,
    quote: OptionQuote | None,
    policy: WheelPolicy,
    *,
    now: datetime,
    session_close: datetime,
) -> bool:
    if contract.underlying != intent.underlying:
        return False
    if contract.right.value != "PUT" or not contract.active or not contract.tradable:
        return False
    dte = (contract.expiration - now.date()).days
    if dte < policy.min_dte_days or dte > policy.max_dte_days:
        return False
    if now >= session_close - timedelta(minutes=policy.preclose_blackout_minutes):
        return False
    if contract.strike > intent.willing_to_own_at_or_below or contract.multiplier <= 0:
        return False
    if quote is None:
        return False
    age = now - quote.as_of
    if age < timedelta(0) or age > timedelta(seconds=policy.max_quote_age_seconds):
        return False
    if quote.bid <= 0 or quote.ask < quote.bid:
        return False
    assignment = contract.strike * contract.multiplier
    premium = quote.bid * contract.multiplier
    return assignment > 0 and premium / assignment >= policy.min_premium_yield_on_assignment


def select_csp(
    intent: OptionIntent,
    contracts: Sequence[OptionContract],
    quote_by_symbol: Mapping[str, OptionQuote],
    policy: WheelPolicy,
    *,
    now: datetime,
    session_close: datetime,
) -> SelectedCsp:
    """Select the closest eligible strike without applying later risk policy."""
    _require_utc(now, "now")
    _require_utc(session_close, "session_close")
    if intent.action is not WheelAction.SELL_CASH_SECURED_PUT:
        raise NoCspSelection("intent is not an opening CSP")

    eligible = [
        contract
        for contract in contracts
        if _eligible(
            intent,
            contract,
            quote_by_symbol.get(contract.occ_symbol),
            policy,
            now=now,
            session_close=session_close,
        )
    ]
    if not eligible:
        raise NoCspSelection("no eligible CSP contract")
    selected = min(
        eligible,
        key=lambda item: (-item.strike, item.expiration, item.occ_symbol),
    )
    quote = quote_by_symbol[selected.occ_symbol]
    contracts_count = 1
    assignment = selected.strike * selected.multiplier * contracts_count
    return SelectedCsp(
        contract=selected,
        quote=quote,
        contracts=contracts_count,
        limit_premium=quote.bid,
        time_in_force="DAY",
        assignment_capital=assignment,
    )
