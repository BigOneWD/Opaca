"""Paper-only submission boundary for one authorized Wheel CSP."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, cast

from opaca.domain.models import AuthorityDecision, AuthorityResult, BrokerEnvironment
from opaca.domain.money import non_negative_money, positive_money
from opaca.wheel.authority import (
    WheelAuthorityContext,
    approval_is_current,
    approval_matches,
    decide_wheel_authority,
)
from opaca.wheel.lifecycle import (
    AuthorizedWheelOrder,
    WheelLifecycleError,
    WheelOrderState,
    record_wheel_order_state,
)
from opaca.wheel.models import OptionContract, OptionQuote, OptionRight, WheelApprovalBinding
from opaca.wheel.order_id import is_valid_wheel_client_order_id, wheel_client_order_id
from opaca.wheel.policy import (
    WheelGuardEngine,
    WheelPolicyContext,
    WheelProposal,
    assignment_capital,
)
from opaca.wheel.reconciliation import WheelBrokerOrder
from opaca.wheel.store import WheelStore

MAX_EXECUTION_QUOTE_AGE = timedelta(seconds=15)


class ExecutionStatus(StrEnum):
    """The submission boundary's deliberately small outcome vocabulary."""

    SUBMITTED = "SUBMITTED"
    NOT_SUBMITTED = "NOT_SUBMITTED"
    UNKNOWN = "UNKNOWN"


class PaperExecutionConfigurationError(ValueError):
    """The concrete gateway is not configured for verified PAPER execution."""


@dataclass(frozen=True)
class OptionOrderRequest:
    """The only order shape admitted by the V1 Wheel execution boundary."""

    symbol: str
    contracts: int
    side: str
    order_type: str
    time_in_force: str
    limit_premium: Decimal
    client_order_id: str

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("option symbol must be non-empty")
        if isinstance(self.contracts, bool) or self.contracts != 1:
            raise ValueError("V1 option execution requires exactly one contract")
        if self.side.upper() != "SELL":
            raise ValueError("V1 option execution only permits SELL")
        if self.order_type.upper() != "LIMIT":
            raise ValueError("V1 option execution only permits LIMIT")
        if self.time_in_force.upper() != "DAY":
            raise ValueError("V1 option execution only permits DAY")
        object.__setattr__(self, "side", self.side.upper())
        object.__setattr__(self, "order_type", self.order_type.upper())
        object.__setattr__(self, "time_in_force", self.time_in_force.upper())
        object.__setattr__(self, "limit_premium", positive_money(self.limit_premium))
        if not is_valid_wheel_client_order_id(self.client_order_id):
            raise ValueError("client_order_id is not a broker-safe Wheel identity")


class OptionExecutionGateway(Protocol):
    """Narrow dependency injected by the execution service."""

    def submit_order(self, request: OptionOrderRequest) -> object:
        """Submit one already-authorized option request."""


@dataclass(frozen=True)
class WheelExecutionAuthorization:
    """Immutable facts bound by the prior policy and reservation decision."""

    order: AuthorizedWheelOrder
    wheel_decision_run_id: str
    attempt_number: int
    proposal: WheelProposal
    assignment_capital: Decimal

    def __post_init__(self) -> None:
        if not self.wheel_decision_run_id.strip():
            raise ValueError("wheel_decision_run_id must be non-empty")
        if isinstance(self.attempt_number, bool) or self.attempt_number < 1:
            raise ValueError("attempt_number must be positive")
        object.__setattr__(self, "assignment_capital", positive_money(self.assignment_capital))


@dataclass(frozen=True)
class WheelExecutionResult:
    """Submission outcome; UNKNOWN always requires later reconciliation."""

    status: ExecutionStatus
    client_order_id: str
    reasons: tuple[str, ...]
    broker_order: object | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))


def _not_submitted(client_order_id: str, *reasons: str) -> WheelExecutionResult:
    return WheelExecutionResult(
        status=ExecutionStatus.NOT_SUBMITTED,
        client_order_id=client_order_id,
        reasons=reasons or ("execution preflight failed",),
    )


def _unknown(
    store: WheelStore,
    client_order_id: str,
    now: datetime,
    *reasons: str,
    broker_order: object | None = None,
) -> WheelExecutionResult:
    try:
        record_wheel_order_state(
            store,
            client_order_id,
            WheelOrderState.UNKNOWN,
            filled_contracts=0,
            exact_occ_position_present=False,
            unresolved_client_order=True,
            now=now,
        )
    except Exception as exc:
        reasons = (*reasons, f"unable to persist UNKNOWN state: {type(exc).__name__}")
    return WheelExecutionResult(
        status=ExecutionStatus.UNKNOWN,
        client_order_id=client_order_id,
        reasons=reasons or ("submission outcome requires reconciliation",),
        broker_order=broker_order,
    )


def _require_utc(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(None):
        raise ValueError("now must be timezone-aware UTC")


def _local_order_and_reservation(
    store: WheelStore,
    authorization: WheelExecutionAuthorization,
    expected_snapshot_version: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    row = store._conn.execute(
        "SELECT client_order_id, occ_symbol, status, reservation_id, assignment_capital, "
        "snapshot_version FROM wheel_orders WHERE client_order_id = ?",
        (authorization.order.client_order_id,),
    ).fetchone()
    if row is None:
        raise WheelLifecycleError("LOCAL_AUTHORIZED_ORDER_MISSING")
    if str(row["client_order_id"]) != authorization.order.client_order_id:
        raise WheelLifecycleError("LOCAL_CLIENT_ORDER_ID_MISMATCH")
    if str(row["occ_symbol"]) != authorization.proposal.contract.occ_symbol:
        raise WheelLifecycleError("LOCAL_OCC_SYMBOL_MISMATCH")
    if str(row["status"]) != WheelOrderState.AUTHORIZED.value:
        raise WheelLifecycleError("LOCAL_ORDER_NOT_AUTHORIZED")
    if row["reservation_id"] != authorization.order.reservation_id:
        raise WheelLifecycleError("LOCAL_RESERVATION_ID_MISMATCH")
    if row["assignment_capital"] is None:
        raise WheelLifecycleError("LOCAL_ASSIGNMENT_CAPITAL_MISSING")
    if non_negative_money(str(row["assignment_capital"])) != authorization.assignment_capital:
        raise WheelLifecycleError("LOCAL_ASSIGNMENT_CAPITAL_MISMATCH")
    if row["snapshot_version"] != expected_snapshot_version:
        raise WheelLifecycleError("LOCAL_SNAPSHOT_VERSION_MISMATCH")
    reservation = store._conn.execute(
        "SELECT reservation_id, underlying, amount, status, kind "
        "FROM wheel_reservations WHERE reservation_id = ?",
        (authorization.order.reservation_id,),
    ).fetchone()
    if reservation is None:
        raise WheelLifecycleError("ACTIVE_ASSIGNMENT_RESERVATION_MISSING")
    if str(reservation["status"]) != "ACTIVE":
        raise WheelLifecycleError("ASSIGNMENT_RESERVATION_NOT_ACTIVE")
    if str(reservation["kind"]) != "CASH_DEPLOYMENT":
        raise WheelLifecycleError("ASSIGNMENT_RESERVATION_KIND_MISMATCH")
    if str(reservation["underlying"]) != authorization.proposal.contract.underlying:
        raise WheelLifecycleError("ASSIGNMENT_RESERVATION_UNDERLYING_MISMATCH")
    if non_negative_money(str(reservation["amount"])) != authorization.assignment_capital:
        raise WheelLifecycleError("ASSIGNMENT_RESERVATION_AMOUNT_MISMATCH")
    return row, reservation


def _approval_expected(
    authorization: WheelExecutionAuthorization,
) -> WheelApprovalBinding:
    return WheelApprovalBinding(
        wheel_decision_run_id=authorization.wheel_decision_run_id,
        attempt_number=authorization.attempt_number,
        occ_symbol=authorization.proposal.contract.occ_symbol,
        action=authorization.proposal.action,
        contracts=authorization.proposal.contracts,
        assignment_capital=authorization.assignment_capital,
        approved_sell_limit_premium=authorization.proposal.sell_limit_premium,
        approved_at=datetime(2000, 1, 1, tzinfo=UTC),
        expires_at=datetime(2000, 1, 1, 0, 5, tzinfo=UTC),
    )


def _response_matches(
    response: object,
    request: OptionOrderRequest,
    contract: OptionContract,
) -> bool:
    if isinstance(response, WheelBrokerOrder):
        return (
            response.client_order_id == request.client_order_id
            and response.occ_symbol == contract.occ_symbol
            and response.side == "SELL"
            and response.right is OptionRight.PUT
            and response.contracts == request.contracts
        )

    def field(name: str) -> object | None:
        if isinstance(response, Mapping):
            return response.get(name)
        return getattr(response, name, None)

    client_order_id = field("client_order_id")
    symbol = field("occ_symbol") or field("symbol")
    side = field("side")
    quantity = field("contracts") or field("qty")
    if not isinstance(client_order_id, str) or not isinstance(symbol, str):
        return False
    if not isinstance(side, str):
        side = getattr(side, "value", None)
    if not isinstance(side, str) or side.upper() != "SELL":
        return False
    if symbol != contract.occ_symbol or client_order_id != request.client_order_id:
        return False
    if quantity is None:
        return False
    try:
        quantity_decimal = Decimal(str(quantity))
    except (ArithmeticError, ValueError):
        return False
    return quantity_decimal == Decimal(request.contracts)


def _fresh_quote(quote: OptionQuote, now: datetime, persisted_limit: Decimal) -> None:
    age = now - quote.as_of
    if age < timedelta(0):
        raise ValueError("AUTHORITATIVE_QUOTE_IN_FUTURE")
    if age > MAX_EXECUTION_QUOTE_AGE:
        raise ValueError("AUTHORITATIVE_QUOTE_STALE")
    if quote.bid <= 0 or quote.ask < quote.bid:
        raise ValueError("AUTHORITATIVE_QUOTE_INVALID")
    if quote.bid < persisted_limit:
        raise ValueError("AUTHORITATIVE_BID_BELOW_PERSISTED_LIMIT")


def _require_approval(
    authorization: WheelExecutionAuthorization,
    approval: object | None,
    now: datetime,
) -> None:
    if not isinstance(approval, WheelApprovalBinding):
        raise ValueError("fresh exact approval is required")
    expected = _approval_expected(authorization)
    if not approval_is_current(approval, now) or not approval_matches(approval, expected):
        raise ValueError("approval is expired or not bound to this order")


def _final_policy_and_authority(
    store: WheelStore,
    authorization: WheelExecutionAuthorization,
    context: WheelPolicyContext,
    authoritative_contract: OptionContract,
    authoritative_quote: OptionQuote,
    now: datetime,
) -> AuthorityDecision:
    persisted_limit = authorization.proposal.sell_limit_premium
    # Re-evaluate economics at the already-persisted limit. A better current
    # bid may pass, but it never widens or lowers the submitted limit.
    final_proposal = replace(
        authorization.proposal,
        contract=authoritative_contract,
        quote=OptionQuote(
            bid=persisted_limit,
            ask=authoritative_quote.ask,
            as_of=authoritative_quote.as_of,
        ),
        sell_limit_premium=persisted_limit,
    )
    active_others = tuple(
        reservation
        for reservation in store.active_assignment_reservations()
        if reservation.reservation_id != authorization.order.reservation_id
    )
    final_context = replace(context, now=now, reservations=active_others)
    decision = WheelGuardEngine().evaluate(final_context, final_proposal)
    if not decision.passed:
        return AuthorityDecision(
            result=AuthorityResult.REJECT,
            reasons=tuple(
                f"{result.check_id.value}: {result.detail}" for result in decision.violations
            ),
            policy_decision=decision,
        )
    capital = authorization.assignment_capital
    by_underlying = sum(
        (
            reservation.amount
            for reservation in active_others
            if reservation.underlying == authoritative_contract.underlying
        ),
        Decimal("0"),
    )
    active_total = sum((reservation.amount for reservation in active_others), Decimal("0"))
    held = context.held_share_exposure.get(authoritative_contract.underlying, Decimal("0"))
    post_name = held + by_underlying + capital
    post_aggregate = (
        sum(context.held_share_exposure.values(), Decimal("0")) + active_total + capital
    )
    return decide_wheel_authority(
        WheelAuthorityContext(
            risk_capital_base=context.risk_capital_base,
            proposed_assignment_capital=capital,
            post_trade_underlying_exposure=post_name,
            post_trade_aggregate_exposure=post_aggregate,
            policy_decision=decision,
            stricter_limits_passed=True,
            policy=context.policy,
        )
    )


def _preflight(
    store: WheelStore,
    authorization: WheelExecutionAuthorization,
    context: object,
    authority: AuthorityDecision,
    *,
    account_id: str,
    expected_snapshot_version: str,
    authoritative_contract: OptionContract,
    authoritative_quote: OptionQuote,
    approval: object | None,
    now: datetime,
) -> OptionOrderRequest:
    _require_utc(now)
    if not isinstance(context, WheelPolicyContext):
        raise ValueError("policy context is not authoritative")
    if authority.result is AuthorityResult.REJECT or not authority.policy_decision.passed:
        raise WheelLifecycleError("authority does not permit submission")
    if context.environment is not BrokerEnvironment.PAPER or not context.environment_verified:
        raise ValueError("PAPER endpoint is not verified")
    if context.kill_switch_active:
        raise ValueError("kill switch is active")
    if not context.account_binding_matches:
        raise ValueError("policy account binding is not current")
    store.assert_account_binding(account_id)
    if store.snapshot_version() != expected_snapshot_version:
        raise ValueError("snapshot version is stale")
    if context.risk_capital_base != store.risk_capital_base():
        raise ValueError("risk capital binding changed")

    _local_order_and_reservation(store, authorization, expected_snapshot_version)
    proposal = authorization.proposal
    if proposal.action.value != "SELL_CASH_SECURED_PUT":
        raise ValueError("proposal is not a CSP")
    if proposal.contract.right is not OptionRight.PUT or proposal.contracts != 1:
        raise ValueError("proposal is not a single-leg opening PUT")
    if authoritative_contract != proposal.contract:
        raise ValueError("authoritative contract identity changed")
    if authorization.order.assignment_capital != authorization.assignment_capital:
        raise ValueError("authorization assignment capital changed")
    if assignment_capital(proposal) != authorization.assignment_capital:
        raise ValueError("proposal assignment capital changed")
    expected_client_order_id = wheel_client_order_id(
        wheel_decision_run_id=authorization.wheel_decision_run_id,
        attempt_number=authorization.attempt_number,
        occ_symbol=proposal.contract.occ_symbol,
        action=proposal.action,
        contracts=proposal.contracts,
        limit_premium=proposal.sell_limit_premium,
    )
    if expected_client_order_id != authorization.order.client_order_id:
        raise ValueError("logical client order identity changed")
    _fresh_quote(authoritative_quote, now, proposal.sell_limit_premium)
    if authority.result is AuthorityResult.APPROVAL_REQUIRED:
        _require_approval(authorization, approval, now)

    final_authority = _final_policy_and_authority(
        store,
        authorization,
        context,
        authoritative_contract,
        authoritative_quote,
        now,
    )
    if final_authority.result is AuthorityResult.REJECT:
        raise WheelLifecycleError("final hard policy rejected submission")
    if final_authority.result is AuthorityResult.APPROVAL_REQUIRED:
        _require_approval(authorization, approval, now)

    request = OptionOrderRequest(
        symbol=proposal.contract.occ_symbol,
        contracts=proposal.contracts,
        side="SELL",
        order_type="LIMIT",
        time_in_force="DAY",
        limit_premium=proposal.sell_limit_premium,
        client_order_id=authorization.order.client_order_id,
    )
    return request


def submit_authorized_csp(
    store: WheelStore,
    gateway: OptionExecutionGateway,
    *,
    account_id: str,
    expected_snapshot_version: str,
    authorization: WheelExecutionAuthorization,
    policy_context: object,
    authority: AuthorityDecision,
    authoritative_contract: OptionContract,
    authoritative_quote: OptionQuote,
    approval: object | None = None,
    now: datetime,
) -> WheelExecutionResult:
    """Run all local gates, then make exactly one injected submit call."""
    client_order_id = authorization.order.client_order_id
    try:
        request = _preflight(
            store,
            authorization,
            policy_context,
            authority,
            account_id=account_id,
            expected_snapshot_version=expected_snapshot_version,
            authoritative_contract=authoritative_contract,
            authoritative_quote=authoritative_quote,
            approval=approval,
            now=now,
        )
    except Exception as exc:
        return _not_submitted(client_order_id, str(exc) or type(exc).__name__)

    try:
        response = gateway.submit_order(request)
    except Exception as exc:
        return _unknown(store, client_order_id, now, f"submit outcome is unknown: {exc}")

    if not _response_matches(response, request, authoritative_contract):
        return _unknown(
            store,
            client_order_id,
            now,
            "broker submit response identity is missing or contradictory",
            broker_order=response,
        )
    try:
        record_wheel_order_state(
            store,
            client_order_id,
            WheelOrderState.SUBMITTED,
            filled_contracts=0,
            exact_occ_position_present=False,
            unresolved_client_order=True,
            now=now,
        )
    except Exception as exc:
        return _unknown(store, client_order_id, now, f"submission state persistence failed: {exc}")
    return WheelExecutionResult(
        status=ExecutionStatus.SUBMITTED,
        client_order_id=client_order_id,
        reasons=("paper CSP submit accepted for later reconciliation",),
        broker_order=response,
    )


class PaperAlpacaOptionExecutionGateway:
    """A narrow adapter for the installed alpaca-py PAPER client."""

    def __init__(
        self,
        *,
        trading_client: object,
        paper: bool,
        paper_endpoint_verified: bool,
    ) -> None:
        if not paper or not paper_endpoint_verified:
            raise PaperExecutionConfigurationError(
                "the Wheel option gateway requires a verified PAPER endpoint"
            )
        submit_order = getattr(trading_client, "submit_order", None)
        if not callable(submit_order):
            raise PaperExecutionConfigurationError("trading client lacks submit_order")
        self._trading_client = trading_client

    def submit_order(self, request: OptionOrderRequest) -> object:
        """Translate exactly one CSP into alpaca-py 0.33.0's order model."""
        from alpaca.trading.enums import OrderSide, OrderType, PositionIntent, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        alpaca_request = LimitOrderRequest(
            symbol=request.symbol,
            qty=request.contracts,
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=float(request.limit_premium),
            client_order_id=request.client_order_id,
            position_intent=PositionIntent.SELL_TO_OPEN,
        )
        return cast(Any, self._trading_client).submit_order(alpaca_request)


__all__ = [
    "ExecutionStatus",
    "OptionExecutionGateway",
    "OptionOrderRequest",
    "PaperAlpacaOptionExecutionGateway",
    "PaperExecutionConfigurationError",
    "WheelExecutionAuthorization",
    "WheelExecutionResult",
    "submit_authorized_csp",
]
