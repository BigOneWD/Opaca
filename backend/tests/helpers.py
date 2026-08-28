"""Shared fixtures and builders.

Broker-shaped fixtures come from the sanitized Phase -1 evidence under
``spike/evidence/`` (read-only, offline). Prices for symbols without Phase -1
fill evidence use fixed deterministic test constants.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast

from opaca.authority.engine import decide_authority
from opaca.calendar.us_trading_calendar import US_TRADING_CALENDAR, TradingCalendar
from opaca.domain.models import (
    AssetState,
    AssetStatus,
    AuthorityDecision,
    AuthorityPolicy,
    AutonomousExecution,
    BrokerCashState,
    BrokerEnvironment,
    ExecutionContext,
    InvestmentPolicy,
    LiquidityPolicy,
    Obligation,
    PolicyDecision,
    Position,
    PrecloseBlackoutConfig,
    Proposal,
    ProposedOrder,
    SettlementEvent,
    Side,
    UnresolvedOrder,
)
from opaca.policy.client_order_id import deterministic_client_order_id
from opaca.policy.engine import PolicyContext, TreasuryGuardEngine
from opaca.treasury.scenario import ScenarioSeed, seed_scenario

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "spike" / "evidence"

#: Deterministic evaluation instant: Tue 2026-09-01 14:30 UTC = 10:30 EDT,
#: mid-session (outside the default pre-close blackout window).
DEFAULT_NOW = datetime(2026, 9, 1, 14, 30, tzinfo=timezone.utc)
DEFAULT_SEED_DATE = date(2026, 9, 1)

#: Phase -1B SGOV fill price (evidence b7). Other symbols have no Phase -1
#: price evidence; fixed deterministic constants are used instead.
PHASE1_SGOV_PRICE = Decimal("100.69")
DEFAULT_PRICES: Mapping[str, Decimal] = {
    "SGOV": PHASE1_SGOV_PRICE,
    "BIL": Decimal("92.00"),
    "SHV": Decimal("110.00"),
}

ENGINE = TreasuryGuardEngine()


def load_evidence(filename: str) -> dict[str, object]:
    raw = json.loads((EVIDENCE_DIR / filename).read_text(encoding="utf-8"))
    return cast(dict[str, object], raw)


def _observations(evidence: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], evidence["observations"])


def phase1_account_fields() -> dict[str, object]:
    evidence = load_evidence("account_20260828T133609Z.json")
    return cast(dict[str, object], _observations(evidence)["account_fields"])


def phase1_broker_cash(
    cash: Decimal | str | int = "100000",
    as_of: datetime = DEFAULT_NOW,
) -> BrokerCashState:
    """BrokerCashState shaped exactly like Phase -1A evidence A1:
    cash 100,000 / buying_power 400,000 / non_marginable 100,000 / 4x."""
    fields = phase1_account_fields()
    return BrokerCashState(
        cash=Decimal(cash) if not isinstance(cash, Decimal) else cash,
        buying_power=Decimal(str(fields["buying_power"])),
        non_marginable_buying_power=Decimal(str(fields["non_marginable_buying_power"])),
        multiplier=Decimal(str(fields["multiplier"])),
        as_of=as_of,
    )


def phase1_asset_states() -> dict[str, AssetState]:
    evidence = load_evidence("assets_20260828T133619Z.json")
    assets = cast(dict[str, dict[str, object]], _observations(evidence)["assets"])
    result = {}
    for symbol, entry in assets.items():
        fields = cast(dict[str, object], entry["fields"])
        result[symbol] = AssetState(
            symbol=symbol,
            status=AssetStatus(str(fields["status"])),
            tradable=bool(fields["tradable"]),
            fractionable=bool(fields["fractionable"]),
        )
    return result


def phase1_calendar_session_dates() -> tuple[date, ...]:
    evidence = load_evidence("calendar_20260828T133740Z.json")
    sessions = cast(list[dict[str, str]], _observations(evidence)["sessions"])
    return tuple(date.fromisoformat(s["date"]) for s in sessions)


def default_liquidity_policy(seed: ScenarioSeed) -> LiquidityPolicy:
    return LiquidityPolicy(operating_reserve=seed.operating_reserve)


def default_investment_policy(
    blackout_enabled: bool = True, minutes_before_close: int = 15
) -> InvestmentPolicy:
    return InvestmentPolicy(
        permitted_symbols=frozenset({"SGOV", "BIL", "SHV"}),
        concentration_max_fraction=Decimal("0.70"),
        min_trade_notional=Decimal("1.00"),
        preclose_blackout=PrecloseBlackoutConfig(
            enabled=blackout_enabled, minutes_before_close=minutes_before_close
        ),
    )


def default_authority_policy() -> AuthorityPolicy:
    return AuthorityPolicy(
        per_order_notional_max=Decimal("25000"),
        per_proposal_notional_max=Decimal("25000"),
        rolling_24h_notional_max=Decimal("50000"),
        rolling_order_count_max=10,
        runaway_hourly_order_count_max=6,
    )


def make_order(
    proposal_id: str,
    leg_index: int,
    symbol: str,
    side: Side,
    quantity: Decimal | str | int,
    reference_price: Decimal | str | int,
) -> ProposedOrder:
    return ProposedOrder(
        proposal_id=proposal_id,
        leg_index=leg_index,
        symbol=symbol,
        side=side,
        quantity=Decimal(quantity) if not isinstance(quantity, Decimal) else quantity,
        reference_price=Decimal(reference_price)
        if not isinstance(reference_price, Decimal)
        else reference_price,
        client_order_id=deterministic_client_order_id(proposal_id, leg_index),
    )


def make_proposal(proposal_id: str, legs: Sequence[ProposedOrder]) -> Proposal:
    return Proposal(proposal_id=proposal_id, legs=tuple(legs))


def make_context(
    *,
    cash: Decimal | str | int = "100000",
    seed_date: date = DEFAULT_SEED_DATE,
    obligations: Sequence[Obligation] | None = None,
    operating_reserve: Decimal | None = None,
    positions: Sequence[Position] = (),
    settlement_events: Sequence[SettlementEvent] = (),
    assets: Mapping[str, AssetState] | None = None,
    prices: Mapping[str, Decimal] | None = None,
    liquidity_policy: LiquidityPolicy | None = None,
    investment_policy: InvestmentPolicy | None = None,
    authority_policy: AuthorityPolicy | None = None,
    now: datetime = DEFAULT_NOW,
    environment: BrokerEnvironment = BrokerEnvironment.PAPER,
    environment_verified: bool = True,
    kill_switch: bool = False,
    unresolved_orders: Sequence[UnresolvedOrder] = (),
    autonomous_history: Sequence[AutonomousExecution] = (),
    calendar: TradingCalendar = US_TRADING_CALENDAR,
    broker: BrokerCashState | None = None,
) -> PolicyContext:
    seed = seed_scenario(cash, seed_date)
    return PolicyContext(
        broker=broker if broker is not None else phase1_broker_cash(cash=cash, as_of=now),
        positions=tuple(positions),
        obligations=tuple(obligations) if obligations is not None else seed.obligations,
        settlement_events=tuple(settlement_events),
        assets=phase1_asset_states() if assets is None else assets,
        prices=DEFAULT_PRICES if prices is None else prices,
        liquidity_policy=liquidity_policy
        if liquidity_policy is not None
        else (
            LiquidityPolicy(operating_reserve=operating_reserve)
            if operating_reserve is not None
            else default_liquidity_policy(seed)
        ),
        investment_policy=investment_policy
        if investment_policy is not None
        else default_investment_policy(),
        authority_policy=authority_policy
        if authority_policy is not None
        else default_authority_policy(),
        execution=ExecutionContext(
            environment=environment,
            environment_verified=environment_verified,
            kill_switch_active=kill_switch,
            now=now,
        ),
        unresolved_orders=tuple(unresolved_orders),
        autonomous_history=tuple(autonomous_history),
        calendar=calendar,
    )


def evaluate(proposal: Proposal, context: PolicyContext) -> PolicyDecision:
    return ENGINE.evaluate(proposal, context)


def decide(proposal: Proposal, context: PolicyContext) -> AuthorityDecision:
    decision = ENGINE.evaluate(proposal, context)
    return decide_authority(
        proposal,
        decision,
        context.authority_policy,
        context.autonomous_history,
        context.execution.now,
    )
