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

Orchestration invariant NOT solved by this stateless engine (RT-01):
``unresolved_orders`` reservations protect against a prior order that is
already recorded in the reconciled input state. Two truly simultaneous
evaluations against the SAME snapshot can still both pass before either
order exists in that snapshot. Any execution layer must therefore perform
``evaluate -> reserve -> persist`` as one ATOMIC single-writer SQLite
transaction BEFORE broker submission; no broker execution may be added
until then. Do not claim the stateless engine alone solves simultaneous
callers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from zoneinfo import ZoneInfo

from opaca.authority.engine import (
    authority_dimension_violations,
    runaway_order_count_violation,
)
from opaca.calendar.us_trading_calendar import CalendarError, TradingCalendar
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
from opaca.domain.money import ZERO, require_positive_decimal, round_money
from opaca.policy.client_order_id import (
    deterministic_client_order_id,
    is_valid_client_order_id,
)
from opaca.treasury.liquidity import (
    LedgerInconsistencyError,
    LiquidityProjection,
    MissingPriceError,
    PortfolioProjection,
    compute_liquidity,
    investment_pool_base,
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
    """All authoritative inputs for one evaluation, reconciled upstream.

    Every reference price must already be a strictly positive finite Decimal
    within money magnitude limits. Float, bool, string, None, NaN, Infinity,
    zero, negative, and oversized values are rejected at this boundary —
    they are never coerced, and they can never reach an AUTO decision.
    """

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

    def __post_init__(self) -> None:
        validated = {
            symbol: require_positive_decimal(price) for symbol, price in self.prices.items()
        }
        object.__setattr__(self, "prices", MappingProxyType(validated))


def _result(check_id: CheckId, passed: bool, detail: str) -> PolicyCheckResult:
    return PolicyCheckResult(
        check_id=check_id, passed=passed, hard=check_id not in SOFT_CHECKS, detail=detail
    )


def sell_reservations(
    unresolved_orders: tuple[UnresolvedOrder, ...],
) -> tuple[dict[str, Decimal], frozenset[str]]:
    """Locally reserved quantity per symbol from unresolved SELL orders
    (RT-01). Every unresolved state counts — pending/new, accepted/live,
    partially filled, UNKNOWN and every pre-submission state — because any
    of them may still consume shares at the broker.

    Returns ``(reserved_by_symbol, undeterminable_symbols)``. A symbol lands
    in ``undeterminable_symbols`` when an unresolved SELL exists whose
    remaining quantity cannot be determined safely; additional sells of that
    symbol must fail closed.
    """
    reserved: dict[str, Decimal] = {}
    undeterminable: set[str] = set()
    for order in unresolved_orders:
        if not order.is_unresolved or order.side is not Side.SELL:
            continue
        remaining = order.remaining_quantity
        if remaining is None:
            undeterminable.add(order.symbol)
        else:
            reserved[order.symbol] = reserved.get(order.symbol, ZERO) + remaining
    return reserved, frozenset(undeterminable)


def effective_available_quantity(
    position: Position | None,
    reserved_quantity: Decimal,
    undeterminable: bool,
) -> Decimal:
    """Reservation-aware long-only bound (RT-01):

        effective_available(symbol) =
            min(
                broker quantity_available,
                reconciled position quantity
                  - locally reserved unresolved SELL remaining quantity
            )

    The ``min`` is essential: Alpaca may already have decremented
    ``quantity_available`` for an acknowledged order, so the local
    reservation must never be subtracted from ``quantity_available`` a
    second time. An undeterminable reservation or a missing position fails
    closed to zero.
    """
    if position is None or undeterminable:
        return ZERO
    local_bound = position.quantity - reserved_quantity
    return min(position.quantity_available, local_bound)


@dataclass(frozen=True)
class _Frame:
    as_of_date: date
    liquidity: LiquidityProjection | None
    proposed_sell_events: tuple[SettlementEvent, ...]
    portfolio: PortfolioProjection | None
    missing_price_symbol: str | None
    buy_notional: Decimal
    projected_quantity_by_symbol: Mapping[str, Decimal]
    sell_quantity_by_symbol: Mapping[str, Decimal]
    calendar_error: str | None = None
    ledger_error: str | None = None


def _ledger_fail(check_id: CheckId, frame: _Frame) -> PolicyCheckResult:
    detail = frame.ledger_error or "liquidity projection unavailable"
    return _result(
        check_id,
        False,
        f"ledger inconsistent: {detail} (fail closed)",
    )


class TreasuryGuardEngine:
    """Stateless deterministic evaluator for SPEC s9 checks."""

    def evaluate(
        self,
        proposal: Proposal,
        context: PolicyContext,
        only: frozenset[CheckId] | None = None,
    ) -> PolicyDecision:
        """Evaluate checks in order. ``only`` restricts evaluation for the
        internal subset evaluator; a partial evaluation is marked
        ``complete=False`` and can never report ``passed=True`` (RT-10)."""
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
                complete=True,
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
        evaluated = {r.check_id for r in results}
        skipped_hard = [
            check_id
            for check_id in CHECK_ORDER
            if check_id not in evaluated and check_id not in SOFT_CHECKS
        ]
        complete = not skipped_hard
        passed = complete and all(r.passed for r in results if r.hard)
        return PolicyDecision(passed=passed, results=tuple(results), complete=complete)

    def _build_frame(self, proposal: Proposal, context: PolicyContext) -> _Frame:
        as_of_date = context.execution.now.date()
        ledger_error: str | None = None
        liquidity: LiquidityProjection | None = None
        try:
            liquidity = compute_liquidity(
                broker=context.broker,
                obligations=context.obligations,
                settlement_events=context.settlement_events,
                operating_reserve=context.liquidity_policy.operating_reserve,
                as_of=as_of_date,
            )
        except LedgerInconsistencyError as exc:
            ledger_error = str(exc)
        calendar_error: str | None = None
        proposed_sell_events: tuple[SettlementEvent, ...] = ()
        try:
            proposed_sell_events = sell_settlement_events(
                proposal.legs, as_of_date, context.calendar
            )
        except CalendarError as exc:
            calendar_error = str(exc)
        missing_price: str | None = None
        portfolio: PortfolioProjection | None = None
        permitted = context.investment_policy.permitted_symbols
        if liquidity is not None:
            try:
                pool_base = investment_pool_base(
                    context.positions, context.prices, liquidity.investable_cash, permitted
                )
                portfolio = project_portfolio(
                    context.positions, proposal.legs, context.prices, pool_base, permitted
                )
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
            calendar_error=calendar_error,
            ledger_error=ledger_error,
        )

    def _post_trade_available(
        self, liquidity: LiquidityProjection, frame: _Frame, day: date
    ) -> Decimal:
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
        liquidity = frame.liquidity
        if liquidity is None:
            return _ledger_fail(CheckId.CHECK_01, frame)
        investable = liquidity.investable_cash
        passed = frame.buy_notional <= investable
        return _result(
            CheckId.CHECK_01,
            passed,
            f"buy notional {frame.buy_notional} vs investable cash {investable}",
        )

    def _check_02(
        self, proposal: Proposal, context: PolicyContext, frame: _Frame
    ) -> PolicyCheckResult:
        liquidity = frame.liquidity
        if liquidity is None:
            return _ledger_fail(CheckId.CHECK_02, frame)
        if frame.calendar_error is not None:
            return _result(
                CheckId.CHECK_02,
                False,
                f"cannot derive proposed-sell settlement dates: {frame.calendar_error} "
                f"(fail closed)",
            )
        reserve = liquidity.operating_reserve
        dates = {frame.as_of_date}
        dates.update(row.on_date for row in liquidity.schedule)
        dates.update(e.settlement_date for e in frame.proposed_sell_events)
        ranked = [(self._post_trade_available(liquidity, frame, day), day) for day in dates]
        if not ranked:
            return _result(
                CheckId.CHECK_02,
                False,
                "cannot determine worst projected liquidity (fail closed)",
            )
        worst_value, worst_day = min(ranked)
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
        """Concentration against the INVESTMENT POOL BASE (Amendment G,
        RT-02): ELIGIBLE investment holdings market value + deployable
        investment cash, fixed at proposal evaluation time. Unfilled
        investment cash stays in the pool, so a partial-fill subset never
        shows a fake 100% concentration; sells reduce concentration and a
        full liquidation passes without any special vacuous branch.

        Non-whitelisted holdings are excluded from the pool and from the
        concentration scope entirely (NEW-01): they buy no headroom and are
        not themselves offenders; CHECK-03 keeps prohibiting trading them.

        Monotonic de-risking (NEW-03, Amendment G clarification): from a
        pre-existing breach, a proposal may pass with the projection still
        above the limit only if every pre-existing offending symbol is
        STRICTLY improved and no previously compliant symbol becomes a new
        offender. The rule is improvement-based, not side-based: there is
        no blanket sell exemption.
        """
        if frame.liquidity is None:
            return _ledger_fail(CheckId.CHECK_04, frame)
        if frame.portfolio is None:
            return _result(
                CheckId.CHECK_04,
                False,
                f"cannot determine concentration: missing reference price for "
                f"{frame.missing_price_symbol} (fail closed)",
            )
        limit = context.investment_policy.concentration_max_fraction
        portfolio = frame.portfolio
        pool = portfolio.investment_pool_base
        if pool <= ZERO:
            if portfolio.total_invested_value <= ZERO:
                return _result(
                    CheckId.CHECK_04, True, "no investment pool base and no holdings; vacuous"
                )
            return _result(
                CheckId.CHECK_04,
                False,
                "investment pool base is zero but projected holdings exist; fail closed",
            )
        permitted = context.investment_policy.permitted_symbols
        projected = portfolio.concentration_by_symbol
        pre_trade: dict[str, Decimal] = {}
        for position in portfolio.positions:
            if position.symbol not in permitted or position.existing_quantity <= ZERO:
                continue
            existing_value = round_money(position.existing_quantity * position.reference_price)
            pre_trade[position.symbol] = existing_value / pool
        pre_offenders = {
            symbol: fraction for symbol, fraction in pre_trade.items() if fraction > limit
        }
        offenders = {symbol: fraction for symbol, fraction in projected.items() if fraction > limit}

        if not offenders:
            return _result(
                CheckId.CHECK_04,
                True,
                f"projected concentration within {limit} against investment pool base "
                f"{pool} (eligible holdings market value + deployable investment cash)",
            )
        if not pre_offenders:
            detail = "; ".join(
                f"{symbol} projected concentration {offenders[symbol]} exceeds {limit} "
                f"of investment pool base {pool}"
                for symbol in sorted(offenders)
            )
            return _result(CheckId.CHECK_04, False, detail)

        problems = []
        for symbol in sorted(pre_offenders):
            projected_fraction = projected.get(symbol, ZERO)
            if not projected_fraction < pre_offenders[symbol]:
                problems.append(
                    f"{symbol} pre-existing breach is not strictly improved "
                    f"(pre-trade {pre_offenders[symbol]}, projected {projected_fraction})"
                )
        for symbol in sorted(set(offenders) - set(pre_offenders)):
            problems.append(
                f"{symbol} was compliant pre-trade but becomes a new offender "
                f"at {offenders[symbol]}"
            )
        if problems:
            return _result(
                CheckId.CHECK_04,
                False,
                "monotonic de-risking not satisfied: " + "; ".join(problems),
            )
        return _result(
            CheckId.CHECK_04,
            True,
            f"monotonic de-risking: every pre-existing breach strictly improved and "
            f"no new offenders against investment pool base {pool} "
            f"(projected may remain above {limit} only while strictly decreasing)",
        )

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
        liquidity = frame.liquidity
        if liquidity is None:
            return _ledger_fail(CheckId.CHECK_06, frame)
        ceiling = liquidity.funding_ceiling
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
        liquidity = frame.liquidity
        if liquidity is None:
            return _ledger_fail(CheckId.CHECK_11, frame)
        if frame.portfolio is None:
            return _result(
                CheckId.CHECK_11,
                False,
                "cannot determine leverage dependence: missing reference price for "
                f"{frame.missing_price_symbol} (fail closed)",
            )
        settled = liquidity.settled_cash
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
        liquidity = frame.liquidity
        if liquidity is None:
            return _ledger_fail(CheckId.CHECK_12, frame)
        if frame.calendar_error is not None:
            return _result(
                CheckId.CHECK_12,
                False,
                f"cannot derive settlement dates for proposed sells: "
                f"{frame.calendar_error}; CHECK-12 cannot pass (fail closed)",
            )
        problems = []
        for obligation in liquidity.obligations:
            available = self._post_trade_available(liquidity, frame, obligation.due_date)
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
        """Market session gate (RT-07): trading-day validity is
        UNCONDITIONAL. A Saturday or exchange holiday fails closed no
        matter how the blackout is configured; the blackout setting
        controls only the optional pre-close window on a valid session."""
        now_local = context.execution.now.astimezone(EXCHANGE_TIMEZONE)
        try:
            session = context.calendar.session(now_local.date())
        except CalendarError:
            session = None
        if session is None:
            return _result(
                CheckId.CHECK_15,
                False,
                f"no trading session on {now_local.date()}; fail closed",
            )
        config = context.investment_policy.preclose_blackout
        if not config.enabled:
            return _result(
                CheckId.CHECK_15,
                True,
                f"trading session on {now_local.date()}; pre-close blackout disabled",
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
        """Long-only with reservation awareness (RT-01).

        Reading only broker ``quantity_available`` is unsafe: an unresolved
        same-direction SELL that the broker has not yet acknowledged (or an
        UNKNOWN order) does not reduce it, so two independent sells could
        each pass and jointly open a short — which the broker allows
        (``shorting_enabled: true``), so the broker is not a backstop. The
        bound is therefore the minimum of the broker figure and the
        reconciled position minus locally reserved unresolved-sell
        remaining quantity, never a double subtraction of the reservation.
        """
        problems = []
        for symbol in sorted(frame.projected_quantity_by_symbol):
            projected = frame.projected_quantity_by_symbol[symbol]
            if projected < ZERO:
                problems.append(f"projected post-trade position negative for {symbol}: {projected}")
        reserved, undeterminable = sell_reservations(context.unresolved_orders)
        positions_by_symbol = {p.symbol: p for p in context.positions}
        for symbol in sorted(frame.sell_quantity_by_symbol):
            sell_quantity = frame.sell_quantity_by_symbol[symbol]
            position = positions_by_symbol.get(symbol)
            if symbol in undeterminable:
                problems.append(
                    f"unresolved SELL of {symbol} has an undeterminable remaining "
                    f"quantity; no additional sell of this symbol is permitted "
                    f"(fail closed)"
                )
                continue
            reserved_quantity = reserved.get(symbol, ZERO)
            available = effective_available_quantity(position, reserved_quantity, False)
            if sell_quantity > available:
                broker_available = position.quantity_available if position is not None else ZERO
                reconciled_quantity = position.quantity if position is not None else ZERO
                problems.append(
                    f"sell quantity {sell_quantity} exceeds reconciled long position "
                    f"available for liquidation {available} ({symbol}); reservation-aware "
                    f"bound = min(broker available {broker_available}, reconciled quantity "
                    f"{reconciled_quantity} - reserved unresolved sells {reserved_quantity}); "
                    f"broker shorting capability is never policy permission"
                )
        passed = not problems
        detail = "long-only invariants hold" if passed else "; ".join(problems)
        return _result(CheckId.CHECK_16, passed, detail)
