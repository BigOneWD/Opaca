"""RED-phase contracts for competition PAPER indicative pricing readiness."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from opaca.__main__ import main
from opaca.domain.models import BrokerEnvironment, CheckId, PolicyDecision
from opaca.wheel import cli
from opaca.wheel.config import WheelPolicy
from opaca.wheel.evidence import build_readiness, render_evidence
from opaca.wheel.models import OptionContract, OptionQuote, OptionRight, WheelAction, WheelState
from opaca.wheel.policy import WheelGuardEngine, WheelPolicyContext, WheelProposal

NOW = datetime(2026, 9, 3, 14, 0, tzinfo=UTC)
CONTRACT = OptionContract(
    occ_symbol="EEM260909P00065000",
    underlying="EEM",
    right=OptionRight.PUT,
    strike=Decimal("65"),
    expiration=date(2026, 9, 9),
    multiplier=Decimal("100"),
    active=True,
    tradable=True,
)


def readiness(*, feed: str, opra_available: bool = False) -> dict[str, object]:
    return build_readiness(
        software_ready=True,
        paper=True,
        feed=feed,
        opra_available=opra_available,
        indicative_available=True,
    )


def evaluate_indicative(quote: OptionQuote) -> PolicyDecision:
    proposal = WheelProposal(
        action=WheelAction.SELL_CASH_SECURED_PUT,
        contract=CONTRACT,
        quote=quote,
        contracts=1,
        sell_limit_premium=quote.bid,
    )
    context = WheelPolicyContext(
        risk_capital_base=Decimal("100000"),
        reconciled_cash=Decimal("100000"),
        held_share_exposure={},
        reservations=(),
        permitted_underlyings=frozenset({"EEM"}),
        wheel_state=WheelState.CASH,
        unresolved_underlyings=frozenset(),
        options_buying_power=Decimal("100000"),
        broker_collateral_consistent=True,
        account_binding_matches=True,
        environment=BrokerEnvironment.PAPER,
        environment_verified=True,
        kill_switch_active=False,
        now=NOW,
        policy=WheelPolicy(),
    )
    return WheelGuardEngine().evaluate(context, proposal)


def test_paper_opra_is_paper_mutation_ready_and_production_grade() -> None:
    result = readiness(feed="opra", opra_available=True)

    assert result["paper_mutation_ready"] is True
    assert result["production_grade_market_data"] is True


def test_paper_fresh_indicative_is_mutation_ready_but_not_production_grade() -> None:
    result = readiness(feed="indicative")

    assert result["paper_mutation_ready"] is True
    assert result["production_grade_market_data"] is False
    assert "accepted for competition PAPER" in str(result["blocker"])
    assert "OPRA" in str(result["blocker"])


def test_readiness_evidence_preserves_indicative_limitation() -> None:
    rendered = render_evidence(readiness(feed="indicative"))

    assert '"paper_mutation_ready": true' in rendered
    assert '"production_grade_market_data": false' in rendered


def test_indicative_quote_at_fifteen_seconds_passes_freshness() -> None:
    decision = evaluate_indicative(
        OptionQuote(bid=Decimal("0.07"), ask=Decimal("0.14"), as_of=NOW - timedelta(seconds=15))
    )

    assert decision.result_for(CheckId.CHECK_19).passed is True


def test_indicative_quote_older_than_fifteen_seconds_fails() -> None:
    decision = evaluate_indicative(
        OptionQuote(bid=Decimal("0.07"), ask=Decimal("0.14"), as_of=NOW - timedelta(seconds=16))
    )

    assert decision.result_for(CheckId.CHECK_19).passed is False


def test_indicative_future_quote_fails() -> None:
    decision = evaluate_indicative(
        OptionQuote(bid=Decimal("0.07"), ask=Decimal("0.14"), as_of=NOW + timedelta(seconds=1))
    )

    assert decision.result_for(CheckId.CHECK_19).passed is False


def test_indicative_nonpositive_bid_fails() -> None:
    decision = evaluate_indicative(
        OptionQuote(bid=Decimal("0"), ask=Decimal("0.14"), as_of=NOW)
    )

    assert decision.result_for(CheckId.CHECK_19).passed is False


def test_indicative_ask_below_bid_fails() -> None:
    decision = evaluate_indicative(
        OptionQuote(bid=Decimal("0.14"), ask=Decimal("0.07"), as_of=NOW)
    )

    assert decision.result_for(CheckId.CHECK_19).passed is False


def test_unknown_feed_fails_closed() -> None:
    result = readiness(feed="unknown")

    assert result["paper_mutation_ready"] is False
    assert result["production_grade_market_data"] is False


def test_indicative_paper_submit_reaches_injected_service_after_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "true")
    monkeypatch.setenv("OPACA_WHEEL_FEED", "indicative")
    monkeypatch.setenv("OPACA_WHEEL_INDICATIVE_AVAILABLE", "true")
    monkeypatch.setenv("OPACA_WHEEL_OPRA_AVAILABLE", "false")
    monkeypatch.setattr(cli, "_paper_gateway_factory", lambda: calls.append("gateway"))
    monkeypatch.setattr(cli, "_paper_submit_service", lambda _gateway: calls.append("service"))

    assert main(["wheel-submit-paper", "--confirm-paper-mutation"]) == 0
    assert calls == ["gateway", "service"]
