"""RED-phase contracts for deterministic CSP selection."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from opaca.wheel.config import WheelPolicy
from opaca.wheel.models import OptionContract, OptionIntent, OptionQuote, OptionRight, WheelAction
from opaca.wheel.selector import NoCspSelection, SelectedCsp, select_csp

NOW = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)
SESSION_CLOSE = datetime(2026, 9, 2, 20, 0, tzinfo=UTC)
INTENT = OptionIntent(
    action=WheelAction.SELL_CASH_SECURED_PUT,
    underlying="SPY",
    market_view="neutral",
    thesis="defined ownership price",
    willing_to_own_at_or_below=Decimal("746"),
    dte_preference=3,
    confidence=Decimal("0.8"),
)


def contract(
    symbol: str,
    strike: str,
    expiration: date,
    *,
    active: bool = True,
    tradable: bool = True,
) -> OptionContract:
    return OptionContract(
        occ_symbol=symbol,
        underlying="SPY",
        right=OptionRight.PUT,
        strike=Decimal(strike),
        expiration=expiration,
        multiplier=Decimal("100"),
        active=active,
        tradable=tradable,
    )


def quote(bid: str, *, as_of: datetime = NOW, ask: str | None = None) -> OptionQuote:
    return OptionQuote(
        bid=Decimal(bid),
        ask=Decimal(ask if ask is not None else bid),
        as_of=as_of,
    )


def test_selector_returns_highest_eligible_strike_then_earliest_expiry() -> None:
    contracts = (
        contract("SPY260904P00746000", "746", date(2026, 9, 4)),
        contract("SPY260903P00745000", "745", date(2026, 9, 3)),
        contract("SPY260903P00746000", "746", date(2026, 9, 3)),
        contract("SPY260903P00750000", "750", date(2026, 9, 3)),
        contract("SPY260903P00740000", "740", date(2026, 9, 3)),
    )
    quotes = {item.occ_symbol: quote("1.00") for item in contracts}

    selected = select_csp(
        INTENT,
        contracts,
        quotes,
        WheelPolicy(),
        now=NOW,
        session_close=SESSION_CLOSE,
    )

    assert isinstance(selected, SelectedCsp)
    assert selected.contract.occ_symbol == "SPY260903P00746000"
    assert selected.contract.expiration == date(2026, 9, 3)
    assert selected.contract.strike == Decimal("746")
    assert selected.contracts == 1
    assert selected.limit_premium == Decimal("1.00")
    assert selected.time_in_force == "DAY"


def test_selector_uses_lexical_occ_symbol_as_final_tie_break() -> None:
    first = contract("SPY260903P00746000-A", "746", date(2026, 9, 3))
    second = contract("SPY260903P00746000-B", "746", date(2026, 9, 3))
    quotes = {first.occ_symbol: quote("1.00"), second.occ_symbol: quote("1.00")}

    selected = select_csp(
        INTENT,
        (second, first),
        quotes,
        WheelPolicy(),
        now=NOW,
        session_close=SESSION_CLOSE,
    )

    assert selected.contract.occ_symbol == first.occ_symbol


@pytest.mark.parametrize(
    "candidate",
    [
        contract("SPY260902P00746000", "746", date(2026, 9, 2)),
        contract("SPY260903P00746000", "746", date(2026, 9, 3), active=False),
        contract("SPY260903P00746000", "746", date(2026, 9, 3), tradable=False),
        contract("SPY260910P00746000", "746", date(2026, 9, 10)),
    ],
)
def test_selector_excludes_zero_dte_expired_or_out_of_window_contracts(
    candidate: OptionContract,
) -> None:
    with pytest.raises(NoCspSelection):
        select_csp(
            INTENT,
            (candidate,),
            {candidate.occ_symbol: quote("1.00")},
            WheelPolicy(),
            now=NOW,
            session_close=SESSION_CLOSE,
        )


def test_selector_fails_closed_inside_final_thirty_minute_blackout() -> None:
    candidate = contract("SPY260903P00746000", "746", date(2026, 9, 3))

    with pytest.raises(NoCspSelection):
        select_csp(
            INTENT,
            (candidate,),
            {candidate.occ_symbol: quote("1.00", as_of=SESSION_CLOSE - timedelta(minutes=20))},
            WheelPolicy(),
            now=SESSION_CLOSE - timedelta(minutes=20),
            session_close=SESSION_CLOSE,
        )


@pytest.mark.parametrize(
    "candidate_quote",
    [
        quote("0.00"),
        quote("1.00", ask="0.50"),
        quote("1.00", as_of=NOW + timedelta(seconds=1)),
        quote("1.00", as_of=NOW - timedelta(seconds=16)),
    ],
)
def test_selector_requires_fresh_executable_quote(candidate_quote: OptionQuote) -> None:
    candidate = contract("SPY260903P00746000", "746", date(2026, 9, 3))

    with pytest.raises(NoCspSelection):
        select_csp(
            INTENT,
            (candidate,),
            {candidate.occ_symbol: candidate_quote},
            WheelPolicy(),
            now=NOW,
            session_close=SESSION_CLOSE,
        )


def test_exact_fifteen_second_quote_age_is_valid() -> None:
    candidate = contract("SPY260903P00746000", "746", date(2026, 9, 3))
    selected = select_csp(
        INTENT,
        (candidate,),
        {candidate.occ_symbol: quote("1.00", as_of=NOW - timedelta(seconds=15))},
        WheelPolicy(),
        now=NOW,
        session_close=SESSION_CLOSE,
    )

    assert selected.quote.as_of == NOW - timedelta(seconds=15)


def test_selector_does_not_move_to_a_lower_strike_for_later_policy_reasons() -> None:
    high = contract("SPY260903P00746000", "746", date(2026, 9, 3))
    lower = contract("SPY260903P00700000", "700", date(2026, 9, 3))

    selected = select_csp(
        INTENT,
        (lower, high),
        {high.occ_symbol: quote("1.00"), lower.occ_symbol: quote("5.00")},
        WheelPolicy(),
        now=NOW,
        session_close=SESSION_CLOSE,
    )

    assert selected.contract == high


def test_selector_requires_csp_action_and_exact_underlying() -> None:
    candidate = contract("SPY260903P00746000", "746", date(2026, 9, 3))
    hold_intent = OptionIntent(
        action=WheelAction.HOLD,
        underlying="SPY",
        market_view="neutral",
        thesis="wait",
        willing_to_own_at_or_below=Decimal("746"),
        dte_preference=3,
        confidence=Decimal("0.8"),
    )
    wrong_underlying = OptionIntent(
        action=WheelAction.SELL_CASH_SECURED_PUT,
        underlying="QQQ",
        market_view="neutral",
        thesis="wrong name",
        willing_to_own_at_or_below=Decimal("746"),
        dte_preference=3,
        confidence=Decimal("0.8"),
    )

    for intent in (hold_intent, wrong_underlying):
        with pytest.raises(NoCspSelection):
            select_csp(
                intent,
                (candidate,),
                {candidate.occ_symbol: quote("1.00")},
                WheelPolicy(),
                now=NOW,
                session_close=SESSION_CLOSE,
            )
