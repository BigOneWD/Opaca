"""Dry Competition Wheel CLI wiring with a structurally separate submit path."""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

from opaca.domain.models import BrokerEnvironment
from opaca.wheel.agent import RepairFeedback, WheelAgentEvaluation, run_wheel_decision
from opaca.wheel.authority import WheelAuthorityContext, decide_wheel_authority
from opaca.wheel.config import WheelPolicy
from opaca.wheel.evidence import build_readiness, render_evidence, sanitize_wheel_evidence
from opaca.wheel.lifecycle import authorize_and_reserve
from opaca.wheel.mcp_guard import MCP_ALLOWED_TOOLS, MCP_REQUIRED_READ_TOOLS
from opaca.wheel.models import (
    OptionContract,
    OptionIntent,
    OptionQuote,
    OptionRight,
    WheelAction,
    WheelState,
)
from opaca.wheel.policy import (
    WheelGuardEngine,
    WheelPolicyContext,
    WheelProposal,
    assignment_capital,
)
from opaca.wheel.reconciliation import reconcile_wheel
from opaca.wheel.selector import select_csp
from opaca.wheel.store import WheelStore

NOW = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)


def _paper_gateway_factory() -> object:
    """Injection point for a future explicitly authorized PAPER command."""
    raise RuntimeError("no PAPER gateway is configured for this dry workflow")


def _paper_submit_service(_gateway: object) -> object:
    """Injection point for Task 10's already-authorized submit service."""
    raise RuntimeError("no PAPER submit service is configured for this dry workflow")


def _paper_environment() -> bool:
    return os.environ.get("ALPACA_PAPER_TRADE", "true").strip().lower() == "true"


def _readiness() -> dict[str, object]:
    return build_readiness(
        software_ready=True,
        paper=_paper_environment(),
        feed=os.environ.get("OPACA_WHEEL_FEED", "indicative"),
        opra_available=os.environ.get("OPACA_WHEEL_OPRA_AVAILABLE", "false")
        .strip()
        .lower()
        == "true",
        indicative_available=os.environ.get("OPACA_WHEEL_INDICATIVE_AVAILABLE", "false")
        .strip()
        .lower()
        == "true",
    )


def _evidence(**fields: object) -> str:
    base: dict[str, object] = {
        "mode": "DRY_RUN / SIMULATED",
        "dry_run": True,
        **fields,
    }
    base.update(_readiness())
    safe = sanitize_wheel_evidence(base)
    return (
        f"SOFTWARE_READY: {'YES' if safe['software_ready'] else 'NO'}\n"
        f"PAPER_MUTATION_READY: {'YES' if safe['paper_mutation_ready'] else 'NO'}\n"
        f"MUTATION_READY: {'YES' if safe['mutation_ready'] else 'NO'}\n"
        "PRODUCTION_GRADE_MARKET_DATA: "
        f"{'YES' if safe['production_grade_market_data'] else 'NO'}\n"
        + render_evidence(safe)
    )


def _probe() -> int:
    from opaca.wheel.mcp_guard import assert_mcp_tool_surface

    assert_mcp_tool_surface(MCP_ALLOWED_TOOLS, MCP_ALLOWED_TOOLS, MCP_REQUIRED_READ_TOOLS)
    readiness = _readiness()
    sys.stdout.write(_evidence(feed=readiness["feed"]))
    return 0


def _dry_contract() -> OptionContract:
    return OptionContract(
        occ_symbol="SPY260904P00100000",
        underlying="SPY",
        right=OptionRight.PUT,
        strike=Decimal("100"),
        expiration=date(2026, 9, 4),
        multiplier=Decimal("100"),
        active=True,
        tradable=True,
    )


def _dry_intent() -> OptionIntent:
    return OptionIntent(
        action=WheelAction.SELL_CASH_SECURED_PUT,
        underlying="SPY",
        market_view="range-bound",
        thesis="defined ownership price",
        willing_to_own_at_or_below=Decimal("100"),
        dte_preference=2,
        confidence=Decimal("0.7"),
    )


def _dry_plan() -> int:
    contract = _dry_contract()
    quote = OptionQuote(bid=Decimal("1"), ask=Decimal("1.05"), as_of=NOW)
    intent = _dry_intent()
    selected = select_csp(
        intent,
        (contract,),
        {contract.occ_symbol: quote},
        WheelPolicy(),
        now=NOW,
        session_close=datetime(2026, 9, 2, 20, 0, tzinfo=UTC),
    )
    proposal = WheelProposal(
        action=intent.action,
        contract=selected.contract,
        quote=selected.quote,
        contracts=selected.contracts,
        sell_limit_premium=selected.limit_premium,
    )
    context = WheelPolicyContext(
        risk_capital_base=Decimal("100000"),
        reconciled_cash=Decimal("10000"),
        held_share_exposure={},
        reservations=(),
        permitted_underlyings=frozenset({"SPY"}),
        wheel_state=WheelState.CASH,
        unresolved_underlyings=frozenset(),
        options_buying_power=Decimal("10000"),
        broker_collateral_consistent=True,
        account_binding_matches=True,
        environment=BrokerEnvironment.PAPER,
        environment_verified=True,
        kill_switch_active=False,
        now=NOW,
        policy=WheelPolicy(),
    )
    decision = WheelGuardEngine().evaluate(context, proposal)
    authority = decide_wheel_authority(
        WheelAuthorityContext(
            risk_capital_base=context.risk_capital_base,
            proposed_assignment_capital=assignment_capital(proposal),
            post_trade_underlying_exposure=assignment_capital(proposal),
            post_trade_aggregate_exposure=assignment_capital(proposal),
            policy_decision=decision,
        )
    )

    class DryProvider:
        def propose(
            self,
            *,
            wheel_decision_run_id: str,
            attempt_number: int,
            repair_feedback: tuple[RepairFeedback, ...],
        ) -> object:
            del wheel_decision_run_id, attempt_number, repair_feedback
            return {
                "action": intent.action.value,
                "underlying": intent.underlying,
                "market_view": intent.market_view,
                "thesis": intent.thesis,
                "willing_to_own_at_or_below": intent.willing_to_own_at_or_below,
                "dte_preference": intent.dte_preference,
                "confidence": intent.confidence,
            }

    agent_result = run_wheel_decision(
        DryProvider(),
        wheel_decision_run_id="dry-plan",
        evaluate_intent=lambda _intent: WheelAgentEvaluation(
            authority_result=authority.result,
            repair_feedback=(),
        ),
    )
    if agent_result.status.value != "COMPLIANT":
        sys.stdout.write(_evidence(blocker=agent_result.status.value))
        return 0
    with tempfile.TemporaryDirectory(prefix="opaca-wheel-plan-") as directory, WheelStore(
        f"{directory}/wheel.sqlite3"
    ) as store:
        store.bootstrap_account("competition-paper", Decimal("100000"), NOW)
        store.set_snapshot_version("dry-snapshot")
        authorized = authorize_and_reserve(
            store,
            account_id="competition-paper",
            expected_snapshot_version="dry-snapshot",
            proposal=proposal,
            policy_context=context,
            authority=authority,
            wheel_decision_run_id="dry-plan",
            attempt_number=1,
            now=NOW,
        )
    sys.stdout.write(
        _evidence(
            underlying=selected.contract.underlying,
            occ_symbol=selected.contract.occ_symbol,
            action=proposal.action.value,
            contracts=proposal.contracts,
            strike=proposal.contract.strike,
            multiplier=proposal.contract.multiplier,
            assignment_capital=selected.assignment_capital,
            authority_result=authority.result.value,
            client_order_id=authorized.client_order_id,
            reserved_assignment_capital=authorized.assignment_capital,
            feed=_readiness()["feed"],
            blocker=None if agent_result.status.value == "COMPLIANT" else agent_result.status.value,
        )
    )
    return 0


def _dry_reconcile() -> int:
    with tempfile.TemporaryDirectory(prefix="opaca-wheel-reconcile-") as directory, WheelStore(
        f"{directory}/wheel.sqlite3"
    ) as store:
        store.bootstrap_account("competition-paper", Decimal("100000"), NOW)
        store.set_snapshot_version("dry-snapshot")
        result = reconcile_wheel(
            store,
            account_id="competition-paper",
            client_order_id="wheel-dry-reconcile",
            expected_occ_symbol="SPY260904P00100000",
            expected_contracts=1,
            expected_assignment_capital=Decimal("10000"),
            expected_multiplier=Decimal("100"),
            broker_order=None,
            option_position=None,
            now=NOW,
        )
        sys.stdout.write(
            _evidence(
                underlying="SPY",
                reconciliation_result=result.status.value,
                wheel_state=result.wheel_state.value,
                reserved_assignment_capital=Decimal("0"),
                feed=_readiness()["feed"],
            )
        )
    return 0


def _submit_paper(args: Sequence[str]) -> int:
    if "--confirm-paper-mutation" not in args:
        sys.stdout.write("EXPLICIT PAPER MUTATION OPT-IN REQUIRED\n")
        return 2
    readiness = _readiness()
    sys.stdout.write(_evidence(feed=readiness["feed"]))
    if readiness["paper_mutation_ready"] is not True:
        return 1
    try:
        gateway = _paper_gateway_factory()
        _paper_submit_service(gateway)
    except Exception as exc:
        sys.stdout.write(f"PAPER SUBMIT BLOCKED: {type(exc).__name__}\n")
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv or ())
    if not args or args[0] in {"-h", "--help"}:
        sys.stdout.write(
            "usage: python -m opaca <wheel-probe|wheel-plan|wheel-reconcile|wheel-submit-paper>\n"
        )
        return 0
    command, rest = args[0], args[1:]
    if command == "wheel-probe":
        return _probe()
    if command == "wheel-plan":
        return _dry_plan()
    if command == "wheel-reconcile":
        return _dry_reconcile()
    if command == "wheel-submit-paper":
        return _submit_paper(rest)
    sys.stderr.write(f"unknown Wheel command {command!r}\n")
    return 2


__all__ = ["main"]
