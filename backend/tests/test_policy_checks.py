"""Individual TreasuryGuard checks: kill switch (12), min trade size (20),
opposing orders (21), pre-close blackout (22), plus CHECK-03/05/08/09/13.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from opaca.domain.models import (
    AssetState,
    AssetStatus,
    AuthorityResult,
    AutonomousExecution,
    BrokerEnvironment,
    CheckId,
    OrderState,
    SettlementEvent,
    Side,
    UnresolvedOrder,
)
from opaca.policy.client_order_id import (
    CLIENT_ORDER_ID_MAX_LENGTH,
    deterministic_client_order_id,
    is_valid_client_order_id,
)
from opaca.treasury.liquidity import LedgerInconsistencyError

from tests.helpers import (
    DEFAULT_NOW,
    decide,
    evaluate,
    make_context,
    make_order,
    make_proposal,
)

PRICE = Decimal("100.00")
PRICES = {"SGOV": PRICE, "BIL": PRICE, "SHV": PRICE}


class TestKillSwitch:
    def test_kill_switch_blocks_all_new_execution_authority(self) -> None:
        """Required proof 12."""
        context = make_context(prices=PRICES, kill_switch=True)
        proposal = make_proposal(
            "prop-kill", [make_order("prop-kill", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        decision = evaluate(proposal, context)
        assert not decision.passed
        assert len(decision.results) == 1
        assert decision.results[0].check_id is CheckId.CHECK_00
        assert decide(proposal, context).result is AuthorityResult.REJECT

    def test_inactive_kill_switch_passes(self) -> None:
        context = make_context(prices=PRICES, obligations=(), operating_reserve=Decimal("0"))
        proposal = make_proposal(
            "prop-alive", [make_order("prop-alive", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        assert evaluate(proposal, context).result_for(CheckId.CHECK_00).passed


class TestMinimumTradeSize:
    def test_notional_below_configured_threshold_is_rejected(self) -> None:
        """Required proof 20."""
        from tests.helpers import default_investment_policy

        policy = default_investment_policy()
        from opaca.domain.models import InvestmentPolicy, PrecloseBlackoutConfig

        strict = InvestmentPolicy(
            permitted_symbols=policy.permitted_symbols,
            concentration_max_fraction=policy.concentration_max_fraction,
            min_trade_notional=Decimal("500.00"),
            preclose_blackout=PrecloseBlackoutConfig(enabled=False, minutes_before_close=15),
        )
        context = make_context(
            prices=PRICES,
            investment_policy=strict,
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-dust", [make_order("prop-dust", 0, "SGOV", Side.BUY, "4", PRICE)]
        )
        assert proposal.legs[0].notional == Decimal("400.00")
        decision = evaluate(proposal, context)
        assert not decision.result_for(CheckId.CHECK_14).passed

    def test_broker_one_dollar_floor_is_enforced_by_policy(self) -> None:
        # Phase -1B B5: Alpaca rejects notionals < $1.00; Opaca enforces its
        # own floor independently of the broker.
        context = make_context(prices=PRICES, obligations=(), operating_reserve=Decimal("0"))
        proposal = make_proposal(
            "prop-floor", [make_order("prop-floor", 0, "SGOV", Side.BUY, "0.005", PRICE)]
        )
        assert proposal.legs[0].notional == Decimal("0.50")
        decision = evaluate(proposal, context)
        assert not decision.result_for(CheckId.CHECK_14).passed


class TestOpposingOrders:
    def test_unresolved_opposing_same_symbol_order_is_rejected(self) -> None:
        """Required proof 21."""
        unresolved = UnresolvedOrder(
            proposal_id="prop-earlier",
            symbol="SGOV",
            side=Side.BUY,
            client_order_id=deterministic_client_order_id("prop-earlier", 0),
            state=OrderState.NEW,
        )
        context = make_context(
            prices=PRICES,
            unresolved_orders=(unresolved,),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-oppose", [make_order("prop-oppose", 0, "SGOV", Side.SELL, "5", PRICE)]
        )
        decision = evaluate(proposal, context)
        result = decision.result_for(CheckId.CHECK_10)
        assert not result.passed
        assert "SGOV" in result.detail

    def test_unknown_state_is_fail_closed_unresolved(self) -> None:
        unresolved = UnresolvedOrder(
            proposal_id="prop-unknown",
            symbol="SGOV",
            side=Side.BUY,
            client_order_id=deterministic_client_order_id("prop-unknown", 0),
            state=OrderState.UNKNOWN_REQUIRES_REVIEW,
        )
        context = make_context(
            prices=PRICES,
            unresolved_orders=(unresolved,),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-oppose-2", [make_order("prop-oppose-2", 0, "SGOV", Side.SELL, "5", PRICE)]
        )
        assert not evaluate(proposal, context).result_for(CheckId.CHECK_10).passed

    def test_same_side_does_not_conflict(self) -> None:
        unresolved = UnresolvedOrder(
            proposal_id="prop-earlier",
            symbol="SGOV",
            side=Side.BUY,
            client_order_id=deterministic_client_order_id("prop-earlier", 0),
            state=OrderState.NEW,
        )
        context = make_context(
            prices=PRICES,
            unresolved_orders=(unresolved,),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-same-side", [make_order("prop-same-side", 0, "SGOV", Side.BUY, "5", PRICE)]
        )
        assert evaluate(proposal, context).result_for(CheckId.CHECK_10).passed

    def test_terminal_states_do_not_conflict(self) -> None:
        for state in (OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED):
            unresolved = UnresolvedOrder(
                proposal_id="prop-done",
                symbol="SGOV",
                side=Side.BUY,
                client_order_id=deterministic_client_order_id("prop-done", 0),
                state=state,
            )
            context = make_context(
                prices=PRICES,
                unresolved_orders=(unresolved,),
                obligations=(),
                operating_reserve=Decimal("0"),
            )
            proposal = make_proposal(
                f"prop-after-{state.value}",
                [make_order(f"prop-after-{state.value}", 0, "SGOV", Side.SELL, "5", PRICE)],
            )
            assert evaluate(proposal, context).result_for(CheckId.CHECK_10).passed

    def test_opposing_legs_within_one_proposal_are_rejected(self) -> None:
        context = make_context(prices=PRICES, obligations=(), operating_reserve=Decimal("0"))
        proposal = make_proposal(
            "prop-self-oppose",
            [
                make_order("prop-self-oppose", 0, "SGOV", Side.BUY, "5", PRICE),
                make_order("prop-self-oppose", 1, "SGOV", Side.SELL, "5", PRICE),
            ],
        )
        assert not evaluate(proposal, context).result_for(CheckId.CHECK_10).passed


class TestPreCloseBlackout:
    def test_blackout_blocks_deterministically_inside_window(self) -> None:
        """Required proof 22 (enabled case). 19:50 UTC = 15:50 EDT, within
        the 15-minute window before the 16:00 close."""
        now = datetime(2026, 9, 1, 19, 50, tzinfo=UTC)
        context = make_context(
            prices=PRICES, now=now, obligations=(), operating_reserve=Decimal("0")
        )
        proposal = make_proposal(
            "prop-blackout", [make_order("prop-blackout", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        result = evaluate(proposal, context).result_for(CheckId.CHECK_15)
        assert not result.passed

    def test_blackout_passes_outside_window(self) -> None:
        now = datetime(2026, 9, 1, 19, 40, tzinfo=UTC)  # 15:40 EDT
        context = make_context(
            prices=PRICES, now=now, obligations=(), operating_reserve=Decimal("0")
        )
        proposal = make_proposal(
            "prop-before-window",
            [make_order("prop-before-window", 0, "SGOV", Side.BUY, "10", PRICE)],
        )
        assert evaluate(proposal, context).result_for(CheckId.CHECK_15).passed

    def test_blackout_can_be_disabled_for_tests(self) -> None:
        """Required proof 22 (configurable/disabled)."""
        from tests.helpers import default_investment_policy

        disabled = default_investment_policy(blackout_enabled=False)
        now = datetime(2026, 9, 1, 19, 55, tzinfo=UTC)
        context = make_context(
            prices=PRICES,
            now=now,
            investment_policy=disabled,
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-disabled", [make_order("prop-disabled", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        assert evaluate(proposal, context).result_for(CheckId.CHECK_15).passed

    def test_blackout_respects_early_close_sessions(self) -> None:
        # Day after Thanksgiving 2026 closes 13:00 EST (UTC-5).
        now = datetime(2026, 11, 27, 17, 50, tzinfo=UTC)  # 12:50 EST
        context = make_context(
            prices=PRICES, now=now, obligations=(), operating_reserve=Decimal("0")
        )
        proposal = make_proposal(
            "prop-early-close",
            [make_order("prop-early-close", 0, "SGOV", Side.BUY, "10", PRICE)],
        )
        assert not evaluate(proposal, context).result_for(CheckId.CHECK_15).passed

    def test_non_trading_day_fails_closed(self) -> None:
        now = datetime(2026, 9, 5, 14, 30, tzinfo=UTC)  # Saturday
        context = make_context(
            prices=PRICES, now=now, obligations=(), operating_reserve=Decimal("0")
        )
        proposal = make_proposal(
            "prop-weekend", [make_order("prop-weekend", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        assert not evaluate(proposal, context).result_for(CheckId.CHECK_15).passed

    def test_saturday_is_rejected_with_blackout_disabled(self) -> None:
        """RT-07: trading-day validity is unconditional; the blackout config
        controls only the pre-close window."""
        from tests.helpers import default_investment_policy

        disabled = default_investment_policy(blackout_enabled=False)
        now = datetime(2026, 9, 5, 14, 30, tzinfo=UTC)  # Saturday
        context = make_context(
            prices=PRICES,
            now=now,
            investment_policy=disabled,
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-sat-off", [make_order("prop-sat-off", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        result = evaluate(proposal, context).result_for(CheckId.CHECK_15)
        assert not result.passed
        assert "fail closed" in result.detail

    def test_exchange_holiday_is_rejected_with_blackout_disabled(self) -> None:
        """RT-07: Labor Day 2026 fails closed even when the optional
        blackout window is disabled."""
        from tests.helpers import default_investment_policy

        disabled = default_investment_policy(blackout_enabled=False)
        now = datetime(2026, 9, 7, 14, 30, tzinfo=UTC)  # Labor Day
        context = make_context(
            prices=PRICES,
            now=now,
            investment_policy=disabled,
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-holiday-off", [make_order("prop-holiday-off", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        decision = evaluate(proposal, context)
        assert not decision.result_for(CheckId.CHECK_15).passed
        assert not decision.passed

    def test_unsupported_calendar_date_fails_closed_for_buys(self) -> None:
        """RT-03/RT-07: a proposal evaluated outside the supported calendar
        range fails closed instead of extrapolating weekdays."""
        now = datetime(2028, 7, 4, 14, 30, tzinfo=UTC)
        context = make_context(
            prices=PRICES, now=now, obligations=(), operating_reserve=Decimal("0")
        )
        proposal = make_proposal(
            "prop-2028", [make_order("prop-2028", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        result = evaluate(proposal, context).result_for(CheckId.CHECK_15)
        assert not result.passed
        assert "fail closed" in result.detail


class TestPermittedSecurity:
    def test_non_whitelisted_symbol_is_rejected(self) -> None:
        context = make_context(
            prices={**PRICES, "AAPL": PRICE}, obligations=(), operating_reserve=Decimal("0")
        )
        proposal = make_proposal(
            "prop-whitelist", [make_order("prop-whitelist", 0, "AAPL", Side.BUY, "10", PRICE)]
        )
        result = evaluate(proposal, context).result_for(CheckId.CHECK_03)
        assert not result.passed
        assert "AAPL" in result.detail


class TestTradability:
    def test_untradable_asset_is_rejected(self) -> None:
        assets = {
            "SGOV": AssetState(
                symbol="SGOV", status=AssetStatus.ACTIVE, tradable=False, fractionable=True
            ),
            "BIL": AssetState(
                symbol="BIL", status=AssetStatus.ACTIVE, tradable=True, fractionable=True
            ),
            "SHV": AssetState(
                symbol="SHV", status=AssetStatus.ACTIVE, tradable=True, fractionable=True
            ),
        }
        context = make_context(
            prices=PRICES, assets=assets, obligations=(), operating_reserve=Decimal("0")
        )
        proposal = make_proposal(
            "prop-tradable", [make_order("prop-tradable", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        assert not evaluate(proposal, context).result_for(CheckId.CHECK_05).passed

    def test_missing_tradability_state_fails_closed(self) -> None:
        context = make_context(
            prices=PRICES, assets={}, obligations=(), operating_reserve=Decimal("0")
        )
        proposal = make_proposal(
            "prop-no-asset", [make_order("prop-no-asset", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        assert not evaluate(proposal, context).result_for(CheckId.CHECK_05).passed


class TestPaperEnvironment:
    def test_live_environment_fails_closed(self) -> None:
        context = make_context(
            prices=PRICES,
            environment=BrokerEnvironment.LIVE,
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-live", [make_order("prop-live", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        decision = evaluate(proposal, context)
        assert not decision.passed
        assert not decision.result_for(CheckId.CHECK_08).passed

    def test_unverified_paper_fails_closed(self) -> None:
        context = make_context(
            prices=PRICES,
            environment_verified=False,
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-unverified", [make_order("prop-unverified", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        assert not evaluate(proposal, context).result_for(CheckId.CHECK_08).passed

    def test_verified_paper_passes(self) -> None:
        context = make_context(prices=PRICES, obligations=(), operating_reserve=Decimal("0"))
        proposal = make_proposal(
            "prop-paper", [make_order("prop-paper", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        assert evaluate(proposal, context).result_for(CheckId.CHECK_08).passed


class TestDeterministicOrderId:
    def test_retry_of_same_leg_produces_same_broker_identifier(self) -> None:
        first = deterministic_client_order_id("prop-9", 0)
        second = deterministic_client_order_id("prop-9", 0)
        assert first == second
        assert is_valid_client_order_id(first)
        assert len(first) <= CLIENT_ORDER_ID_MAX_LENGTH
        assert deterministic_client_order_id("prop-9", 1) != first

    def test_tampered_client_order_id_is_rejected(self) -> None:
        from opaca.domain.models import ProposedOrder

        leg = ProposedOrder(
            proposal_id="prop-tamper",
            leg_index=0,
            symbol="SGOV",
            side=Side.BUY,
            quantity=Decimal("10"),
            reference_price=PRICE,
            client_order_id="not-the-deterministic-id",
        )
        context = make_context(prices=PRICES, obligations=(), operating_reserve=Decimal("0"))
        proposal = make_proposal("prop-tamper", [leg])
        assert not evaluate(proposal, context).result_for(CheckId.CHECK_09).passed

    def test_duplicate_leg_index_is_rejected(self) -> None:
        context = make_context(prices=PRICES, obligations=(), operating_reserve=Decimal("0"))
        proposal = make_proposal(
            "prop-dup",
            [
                make_order("prop-dup", 0, "SGOV", Side.BUY, "5", PRICE),
                make_order("prop-dup", 0, "BIL", Side.BUY, "5", PRICE),
            ],
        )
        assert not evaluate(proposal, context).result_for(CheckId.CHECK_09).passed

    def test_well_formed_proposal_passes_check_09(self) -> None:
        context = make_context(prices=PRICES, obligations=(), operating_reserve=Decimal("0"))
        proposal = make_proposal(
            "prop-ok", [make_order("prop-ok", 0, "SGOV", Side.BUY, "5", PRICE)]
        )
        assert evaluate(proposal, context).result_for(CheckId.CHECK_09).passed


class TestRunawayLimit:
    def test_runaway_hourly_limit_is_hard_reject(self) -> None:
        history = tuple(
            AutonomousExecution(
                timestamp=DEFAULT_NOW - timedelta(minutes=10), notional=Decimal("100")
            )
            for _ in range(6)
        )
        context = make_context(
            prices=PRICES,
            autonomous_history=history,
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-runaway", [make_order("prop-runaway", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        decision = evaluate(proposal, context)
        assert not decision.result_for(CheckId.CHECK_13).passed
        assert not decision.passed

    def test_within_runaway_limit_passes(self) -> None:
        history = tuple(
            AutonomousExecution(
                timestamp=DEFAULT_NOW - timedelta(minutes=10), notional=Decimal("100")
            )
            for _ in range(5)
        )
        context = make_context(
            prices=PRICES,
            autonomous_history=history,
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-runaway-2", [make_order("prop-runaway-2", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        assert evaluate(proposal, context).result_for(CheckId.CHECK_13).passed

    def test_hour_window_excludes_older_executions(self) -> None:
        history = tuple(
            AutonomousExecution(
                timestamp=DEFAULT_NOW - timedelta(minutes=61), notional=Decimal("100")
            )
            for _ in range(6)
        )
        context = make_context(
            prices=PRICES,
            autonomous_history=history,
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-runaway-3", [make_order("prop-runaway-3", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        assert evaluate(proposal, context).result_for(CheckId.CHECK_13).passed


class TestLedgerInconsistencyFailsClosed:
    def test_evaluate_and_decide_do_not_raise_and_reject(self) -> None:
        event = SettlementEvent(
            event_id="inconsistent",
            symbol="SGOV",
            trade_date=date(2026, 8, 28),
            settlement_date=date(2026, 9, 2),
            amount=Decimal("5000.00"),
        )
        context = make_context(
            prices=PRICES,
            cash=Decimal("1000.00"),
            settlement_events=(event,),
            obligations=(),
            operating_reserve=Decimal("0"),
        )
        proposal = make_proposal(
            "prop-ledger", [make_order("prop-ledger", 0, "SGOV", Side.BUY, "1", PRICE)]
        )
        try:
            decision = evaluate(proposal, context)
        except LedgerInconsistencyError:
            pytest.fail("LedgerInconsistencyError escaped evaluate()")
        for check_id in (CheckId.CHECK_01, CheckId.CHECK_02, CheckId.CHECK_11):
            result = decision.result_for(check_id)
            assert not result.passed
            assert "ledger inconsistent" in result.detail
            assert "fail closed" in result.detail
        assert not decision.passed
        try:
            authority = decide(proposal, context)
        except LedgerInconsistencyError:
            pytest.fail("LedgerInconsistencyError escaped decide()")
        assert authority.result is AuthorityResult.REJECT

    def test_check_02_still_reports_worst_projected_liquidity(self) -> None:
        context = make_context(prices=PRICES, obligations=(), operating_reserve=Decimal("0"))
        proposal = make_proposal(
            "prop-check02", [make_order("prop-check02", 0, "SGOV", Side.BUY, "1", PRICE)]
        )
        result = evaluate(proposal, context).result_for(CheckId.CHECK_02)
        assert result.passed
        assert "worst projected liquidity" in result.detail


class TestMissingPriceFailsClosed:
    def test_missing_reference_price_blocks_concentration_and_leverage_checks(self) -> None:
        context = make_context(
            prices={"BIL": PRICE}, obligations=(), operating_reserve=Decimal("0")
        )
        proposal = make_proposal(
            "prop-noprice", [make_order("prop-noprice", 0, "SGOV", Side.BUY, "10", PRICE)]
        )
        decision = evaluate(proposal, context)
        assert not decision.result_for(CheckId.CHECK_04).passed
        assert not decision.result_for(CheckId.CHECK_11).passed
        assert not decision.passed


class TestPartialEvaluationCannotReportCompletePass:
    """RT-10: evaluate(only=...) must never silently return a complete
    PolicyDecision.passed=True while hard checks were intentionally
    skipped. The internal subset evaluator keeps working because it reads
    results/violations, not passed."""

    def test_partial_evaluation_is_incomplete_and_cannot_pass(self) -> None:
        from opaca.policy.engine import TreasuryGuardEngine

        context = make_context(prices=PRICES)
        proposal = make_proposal(
            "prop-partial", [make_order("prop-partial", 0, "SGOV", Side.BUY, "184.8", PRICE)]
        )
        full = evaluate(proposal, context)
        assert full.complete
        assert not full.passed  # CHECK-04: 18,480 is 84% of the 22,000 pool
        narrow = TreasuryGuardEngine().evaluate(
            proposal, context, only=frozenset({CheckId.CHECK_03})
        )
        assert not narrow.complete
        assert not narrow.passed
        assert narrow.result_for(CheckId.CHECK_03).passed

    def test_complete_evaluation_keeps_passed_semantics(self) -> None:
        context = make_context(prices=PRICES, obligations=(), operating_reserve=Decimal("0"))
        proposal = make_proposal(
            "prop-complete", [make_order("prop-complete", 0, "SGOV", Side.BUY, "1", PRICE)]
        )
        decision = evaluate(proposal, context)
        assert decision.complete
        assert decision.passed

    def test_partial_results_still_readable_by_the_subset_evaluator(self) -> None:
        from opaca.policy.engine import TreasuryGuardEngine

        context = make_context(prices=PRICES, obligations=(), operating_reserve=Decimal("0"))
        proposal = make_proposal(
            "prop-readable", [make_order("prop-readable", 0, "AAPL", Side.BUY, "1", PRICE)]
        )
        narrow = TreasuryGuardEngine().evaluate(
            proposal, context, only=frozenset({CheckId.CHECK_03})
        )
        assert tuple(r.check_id for r in narrow.violations) == (CheckId.CHECK_03,)

    def test_kill_switch_short_circuit_remains_complete_reject(self) -> None:
        context = make_context(prices=PRICES, kill_switch=True)
        proposal = make_proposal(
            "prop-kill-complete",
            [make_order("prop-kill-complete", 0, "SGOV", Side.BUY, "1", PRICE)],
        )
        decision = evaluate(proposal, context)
        assert decision.complete
        assert not decision.passed


@pytest.mark.parametrize("state", list(OrderState))
def test_order_state_enum_covers_spec_section_13(state: OrderState) -> None:
    assert state.value == state.name
