"""TreasuryGuard policy engine (SPEC s9, frozen spec wins).

Design rules implemented here:

* All inputs are typed domain models; no live broker calls in this phase.
* The engine is pure/stateless: identical inputs always yield identical
  decisions (determinism).
* Fail-closed: anything the engine cannot determine (missing price, missing
  tradability state, no session for blackout arithmetic) is a violation.
* CHECK-07 is an AUTHORITY INPUT, not a hard rejection: policy-valid
  proposals outside delegated authority become APPROVAL_REQUIRED, never
  REJECT, via the authority engine.
* The engine never reads ``BrokerCashState.buying_power``: Phase -1A proved
  it is 4x reconciled cash (broker leverage), not corporate liquidity
  (CHECK-06 / CHECK-11).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from opaca.authority.engine import (
    authority_dimension_violations,
    runaway_order_count_violation,
)
from opaca.calendar.us_trading_calendar import TradingCalendar
from opaca.domain.models import (
    AssetState,
    AuthorityPolicy,
    AutonomousExecution,
    BrokerCashState,
    BrokerEnvironment,
    CheckId,
    ExecutionContext,
    InvestmentPolicy,
    LiquidityPolicy,
    Obligation,
    PolicyCheckResult,
    PolicyDecision,
    Position,
    Proposal,
    SettlementEvent,
    Side,
    UnresolvedOrder,
)
from opaca.domain.money import ZERO
from opaca.policy.client_order_id import (
    deterministic_client_order_id,
    is_valid_client_order_id,
)
from opaca.treasury.liquidity import (
    LiquidityProjection,
    MissingPriceError,
    PortfolioProjection,
    compute_liquidity,
    project_portfolio,
    sell_settlement_events,
)

EXCHANGE_TIMEZONE = ZoneInfo("America/New_York")

#: Evaluation order. CHECK-00 short-circuits (SPEC: NO NEW ORDER MAY BE
#: SUBMITTED). CHECK-07 is soft (authority input); every other check is hard.
CHECK_ORDER: tuple[CheckId, ...] = (
    CheckId.CHECK_00,
    CheckId.CHECK_01,
    CheckId.CHECK_02,
    CheckId.CHECK_03,
    CheckId.CHECK_04,
    CheckId.CHECK_05,
    CheckId.CHECK_06,
    CheckId.CHECK_07,
    CheckId.CHECK_08,
    CheckId.CHECK_09,
    CheckId.CHECK_10,
    CheckId.CHECK_11,
    CheckId.CHECK_12,
    CheckId.CHECK_13,
    CheckId.CHECK_14,
    CheckId.CHECK_15,
    CheckId.CHECK_16,
)

SOFT_CHECKS: frozenset[CheckId] = frozenset({CheckId.CHECK_07})


@dataclass(frozen=True)
class PolicyContext:
    """All authoritative inputs for one evaluation, reconciled upstream."""

    broker: BrokerCashState
    positions: tuple[Position, ...]
    obligations: tuple[Obligation, ...]
    settlement_events: tuple[SettlementEvent, ...]
    assets: Mapping[str, AssetState]
    prices: Mapping[str, Decimal]
    liquidity_policy: LiquidityPolicy
    investment_policy: InvestmentPolicy
    authority_policy: AuthorityPolicy
    execution: ExecutionContext
    unresolved_orders: tuple[UnresolvedOrder, ...]
    autonomous_history: tuple[AutonomousExecution, ...]
    calendar: TradingCalendar


def _result(check_id: CheckId, passed: bool, detail: str) -> PolicyCheckResult:
    return PolicyCheckResult(
        check_id=check_id, passed=passed, hard=check_id not in SOFT_CHECKS, detail=detail
    )


@dataclass(frozen=True)
class _Frame:
    as_of_date: date
    liquidity: LiquidityProjection
    proposed_sell_events: tuple[SettlementEvent, ...]
    portfolio: PortfolioProjection | None
    missing_price_symbol: str | None
    buy_notional: Decimal
    projected_quantity_by_symbol: Mapping[str, Decimal]
    sell_quantity_by_symbol: Mapping[str, Decimal]


class TreasuryGuardEngine:
    """Stateless deterministic evaluator for SPEC s9 checks."""

    def evaluate(
        self,
        proposal: Proposal,
        context: PolicyContext,
        only: frozenset[CheckId] | None = None,
    ) -> PolicyDecision:
        if context.execution.kill_switch_active:
            return PolicyDecision(
                passed=False,
                results=(
                    _result(
                        CheckId.CHECK_00,
                        False,
                        "kill switch active: no new order may be submitted",
                    ),
                ),
            )

        frame = self._build_frame(proposal, context)
        results = []
        for check_id in CHECK_ORDER:
            if only is not None and check_id not in only:
                continue
            handler: Callable[[Proposal, PolicyContext, _Frame], PolicyCheckResult] = getattr(
                self, f"_check_{check_id.name.split('_')[1]}"
            )
            results.append(handler(proposal, context, frame))
        passed = all(r.passed for r in results if r.hard)
        return PolicyDecision(passed=passed, results=tuple(results))

    def _build_frame(self, proposal: Proposal, context: PolicyContext) -> _Frame:
        as_of_date = context.execution.now.date()
        liquidity = compute_liquidity(
            broker=context.broker,
            obligations=context.obligations,
            settlement_events=context.settlement_events,
            operating_reserve=context.liquidity_policy.operating_reserve,
            as_of=as_of_date,
        )
        proposed_sell_events = sell_settlement_events(proposal.legs, as_of_date, context.calendar)
        missing_price: str | None = None
        portfolio: PortfolioProjection | None = None
        try:
            portfolio = project_portfolio(context.positions, proposal.legs, context.prices)
        except MissingPriceError as exc:
            missing_price = str(exc.args[0]) if exc.args else "unknown"

        existing = {p.symbol: p.quantity for p in context.positions}
        projected_qty: dict[str, Decimal] = dict(existing)
        sell_qty: dict[str, Decimal] = {}
        for leg in proposal.legs:
            if leg.side is Side.BUY:
                projected_qty[leg.symbol] = projected_qty.get(leg.symbol, ZERO) + leg.quantity
            else:
                projected_qty[leg.symbol] = projected_qty.get(leg.symbol, ZERO) - leg.quantity
                sell_qty[leg.symbol] = sell_qty.get(leg.symbol, ZERO) + leg.quantity

        return _Frame(
            as_of_date=as_of_date,
            liquidity=liquidity,
            proposed_sell_events=proposed_sell_events,
            portfolio=portfolio,
            missing_price_symbol=missing_price,
            buy_notional=proposal.total_buy_notional,
            projected_quantity_by_symbol=projected_qty,
            sell_quantity_by_symbol=sell_qty,
        )

    def _post_trade_available(self, frame: _Frame, day: date) -> Decimal:
        liquidity = frame.liquidity
        extra = sum(
            (e.amount for e in frame.proposed_sell_events if e.settlement_date <= day), ZERO
        )
        return (
            liquidity.settled_cash
            - frame.buy_notional
            + liquidity.proceeds_settling_by(day)
            + extra
            - liquidity.obligations_due_by(day)
        )

    def _check_00(
        self, proposal: Proposal, context: PolicyContext, frame: _Frame
    ) -> PolicyCheckResult:
        return _result(CheckId.CHECK_00, True, "kill switch inactive")

    def _check_01(
        self, proposal: Proposal, context: PolicyContext, frame: _Frame
    ) -> PolicyCheckResult:
        investable = frame.liquidity.investable_cash
        passed = frame.buy_notional <= investable
        return _result(
            CheckId.CHECK_01,
            passed,
            f"buy notional {frame.buy_notional} vs investable cash {investable}",
        )

    def _check_02(
        self, proposal: Proposal, context: PolicyContext, frame: _Frame
    ) -> PolicyCheckResult:
        reserve = frame.liquidity.operating_reserve
        dates = {frame.as_of_date}
        dates.update(row.on_date for row in frame.liquidity.schedule)
        dates.update(e.settlement_date for e in frame.proposed_sell_events)
        worst_day = None
        worst_value = None
        for day in sorted(dates):
            available = self._post_trade_available(frame, day)
            if worst_value is None or available < worst_value:
                worst_value = available
                worst_day = day
        assert worst_value is not None
        passed = worst_value >= reserve
        return _result(
            CheckId.CHECK_02,
            passed,
            f"worst projected liquidity {worst_value} on {worst_day} "
            f"vs operating reserve {reserve}",
        )

    def _check_03(
        self, proposal: Proposal, context: PolicyContext, frame: _Frame
    ) -> PolicyCheckResult:
        permitted = context.investment_policy.permitted_symbols
        offenders = sorted(symbol for symbol in proposal.symbols if symbol not in permitted)
        passed = not offenders
        detail = "all symbols on whitelist" if passed else f"symbols not whitelisted: {offenders}"
        return _result(CheckId.CHECK_03, passed, detail)

    def _check_04(
        self, proposal: Proposal, context: PolicyContext, frame: _Frame
    ) -> PolicyCheckResult:
        if frame.portfolio is None:
            return _result(
                CheckId.CHECK_04,
                False,
                f"cannot determine concentration: missing reference price for "
                f"{frame.missing_price_symbol} (fail closed)",
            )
        limit = context.investment_policy.concentration_max_fraction
        portfolio = frame.portfolio
        if portfolio.total_invested_value <= ZERO:
            return _result(CheckId.CHECK_04, True, "no projected invested market value; vacuous")
        offenders = sorted(
            symbol
            for symbol, fraction in portfolio.concentration_by_symbol.items()
            if fraction > limit
        )
        passed = not offenders
        if passed:
            detail = (
                f"projected post-trade concentration within {limit} "
                f"(denominator {portfolio.total_invested_value})"
            )
        else:
            detail = "; ".join(
                f"{symbol} projected concentration "
                f"{portfolio.concentration_by_symbol[symbol]} exceeds {limit}"
                for symbol in offenders
            )
        return _result(CheckId.CHECK_04, passed, detail)

    def _check_05(
        self, proposal: Proposal, context: PolicyContext, frame: _Frame
    ) -> PolicyCheckResult:
        offenders = []
        for symbol in sorted(proposal.symbols):
            asset = context.assets.get(symbol)
            if asset is None:
                offenders.append(f"{symbol} (no tradability state; fail closed)")
            elif not asset.is_permitted_for_trading:
                offenders.append(
                    f"{symbol} (status={asset.status.value}, tradable={asset.tradable})"
                )
        passed = not offenders
        detail = "all assets tradable" if passed else f"not tradable: {offenders}"
        return _result(CheckId.CHECK_05, passed, detail)

    def _check_06(
        self, proposal: Proposal, context: PolicyContext, frame: _Frame
    ) -> PolicyCheckResult:
        ceiling = frame.liquidity.funding_ceiling
        passed = frame.buy_notional <= ceiling
        return _result(
            CheckId.CHECK_06,
            passed,
            f"buy notional {frame.buy_notional} vs funding ceiling {ceiling} "
            f"(derived from reconciled cash only; broker buying_power ignored)",
        )

    def _check_07(
        self, proposal: Proposal, context: PolicyContext, frame: _Frame
    ) -> PolicyCheckResult:
        violations = authority_dimension_violations(
            proposal,
            context.authority_policy,
            context.autonomous_history,
            context.execution.now,
        )
        passed = not violations
        detail = (
            "all delegated-authority dimensions pass"
            if passed
            else "authority input: " + "; ".join(violations)
        )
        return _result(CheckId.CHECK_07, passed, detail)

    def _check_08(
        self, proposal: Proposal, context: PolicyContext, frame: _Frame
    ) -> PolicyCheckResult:
        execution = context.execution
        passed = execution.environment is BrokerEnvironment.PAPER and execution.environment_verified
        if passed:
            detail = "paper environment verified"
        elif execution.environment is not BrokerEnvironment.PAPER:
            detail = f"environment is {execution.environment.value}; execution must fail closed"
        else:
            detail = "paper environment not verified; execution must fail closed"
        return _result(CheckId.CHECK_08, passed, detail)

    def _check_09(
        self, proposal: Proposal, context: PolicyContext, frame: _Frame
    ) -> PolicyCheckResult:
        problems = []
        seen_legs: set[int] = set()
        for leg in proposal.legs:
            if leg.leg_index in seen_legs:
                problems.append(f"duplicate leg_index {leg.leg_index}")
            seen_legs.add(leg.leg_index)
            expected = deterministic_client_order_id(proposal.proposal_id, leg.leg_index)
            if leg.client_order_id != expected:
                problems.append(
                    f"leg {leg.leg_index} client_order_id is not the deterministic "
                    f"hash of proposal_id+leg_index"
                )
            elif not is_valid_client_order_id(leg.client_order_id):
                problems.append(f"leg {leg.leg_index} client_order_id violates Alpaca constraints")
        passed = not problems
        detail = (
            "deterministic logical-order identity holds"
            if passed
            else "CHECK-09 violations: " + "; ".join(problems)
        )
        return _result(CheckId.CHECK_09, passed, detail)

    def _check_10(
        self, proposal: Proposal, context: PolicyContext, frame: _Frame
    ) -> PolicyCheckResult:
        problems = []
        buy_symbols = {leg.symbol for leg in proposal.buy_legs}
        sell_symbols = {leg.symbol for leg in proposal.sell_legs}
        internal = sorted(buy_symbols & sell_symbols)
        if internal:
            problems.append(f"proposal contains opposing sides for {internal}")
        for order in context.unresolved_orders:
            if not order.is_unresolved:
                continue
            if order.symbol in proposal.symbols:
                proposal_sides = {leg.side for leg in proposal.legs if leg.symbol == order.symbol}
                opposing = Side.SELL if order.side is Side.BUY else Side.BUY
                if opposing in proposal_sides:
                    problems.append(
                        f"unresolved {order.side.value} {order.symbol} "
                        f"(proposal {order.proposal_id}, state {order.state.value}) "
                        f"opposes this proposal"
                    )
        passed = not problems
        detail = "no opposing unresolved orders" if passed else "; ".join(problems)
        return _result(CheckId.CHECK_10, passed, detail)

    def _check_11(
        self, proposal: Proposal, context: PolicyContext, frame: _Frame
    ) -> PolicyCheckResult:
        if frame.portfolio is None:
            return _result(
                CheckId.CHECK_11,
                False,
                "cannot determine leverage dependence: missing reference price for "
                f"{frame.missing_price_symbol} (fail closed)",
            )
        settled = frame.liquidity.settled_cash
        passed = frame.buy_notional <= settled
        return _result(
            CheckId.CHECK_11,
            passed,
            f"buy notional {frame.buy_notional} vs reconciled settled cash {settled} "
            f"(anything beyond settled cash would be margin leverage)",
        )

    def _check_12(
        self, proposal: Proposal, context: PolicyContext, frame: _Frame
    ) -> PolicyCheckResult:
        if not proposal.sell_legs:
            return _result(CheckId.CHECK_12, True, "no sell legs; vacuous")
        problems = []
        for obligation in frame.liquidity.obligations:
            available = self._post_trade_available(frame, obligation.due_date)
            if available < ZERO:
                settlement_dates = sorted({e.settlement_date for e in frame.proposed_sell_events})
                problems.append(
                    f"obligation {obligation.obligation_id} due {obligation.due_date}: "
                    f"derived available liquidity {available} < 0; proposed proceeds "
                    f"settle {settlement_dates} (T+1 derived schedule, independent of "
                    f"paper cash crediting)"
                )
        passed = not problems
        detail = (
            "all obligations funded before due date on the derived settlement schedule"
            if passed
            else "; ".join(problems)
        )
        return _result(CheckId.CHECK_12, passed, detail)

    def _check_13(
        self, proposal: Proposal, context: PolicyContext, frame: _Frame
    ) -> PolicyCheckResult:
        violation = runaway_order_count_violation(
            proposal,
            context.authority_policy,
            context.autonomous_history,
            context.execution.now,
        )
        passed = violation is None
        return _result(CheckId.CHECK_13, passed, violation if violation else "within runaway limit")

    def _check_14(
        self, proposal: Proposal, context: PolicyContext, frame: _Frame
    ) -> PolicyCheckResult:
        minimum = context.investment_policy.min_trade_notional
        offenders = sorted(
            f"leg {leg.leg_index} {leg.symbol} notional {leg.notional}"
            for leg in proposal.legs
            if leg.notional < minimum
        )
        passed = not offenders
        detail = (
            f"all legs >= minimum trade size {minimum}"
            if passed
            else f"dust trades below {minimum}: {offenders}"
        )
        return _result(CheckId.CHECK_14, passed, detail)

    def _check_15(
        self, proposal: Proposal, context: PolicyContext, frame: _Frame
    ) -> PolicyCheckResult:
        config = context.investment_policy.preclose_blackout
        if not config.enabled:
            return _result(CheckId.CHECK_15, True, "pre-close blackout disabled")
        now_local = context.execution.now.astimezone(EXCHANGE_TIMEZONE)
        session = context.calendar.session(now_local.date())
        if session is None:
            return _result(
                CheckId.CHECK_15,
                False,
                f"no trading session on {now_local.date()}; fail closed",
            )
        close_local = datetime.combine(
            session.session_date, session.close_time, tzinfo=EXCHANGE_TIMEZONE
        )
        window_start = close_local - timedelta(minutes=config.minutes_before_close)
        if window_start <= now_local <= close_local:
            return _result(
                CheckId.CHECK_15,
                False,
                f"within pre-close blackout window ({config.minutes_before_close} min "
                f"before {session.close_time} close)",
            )
        return _result(CheckId.CHECK_15, True, "outside pre-close blackout window")

    def _check_16(
        self, proposal: Proposal, context: PolicyContext, frame: _Frame
    ) -> PolicyCheckResult:
        problems = []
        available_by_symbol = {p.symbol: p.quantity_available for p in context.positions}
        for symbol in sorted(frame.projected_quantity_by_symbol):
            projected = frame.projected_quantity_by_symbol[symbol]
            if projected < ZERO:
                problems.append(f"projected post-trade position negative for {symbol}: {projected}")
        for symbol in sorted(frame.sell_quantity_by_symbol):
            sell_quantity = frame.sell_quantity_by_symbol[symbol]
            available = available_by_symbol.get(symbol, ZERO)
            if sell_quantity > available:
                problems.append(
                    f"sell quantity {sell_quantity} exceeds reconciled long position "
                    f"available for liquidation {available} ({symbol}); broker shorting "
                    f"capability is never policy permission"
                )
        passed = not problems
        detail = "long-only invariants hold" if passed else "; ".join(problems)
        return _result(CheckId.CHECK_16, passed, detail)
