"""RED-phase contracts for the bounded, non-broker Wheel intent agent."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any, cast

import pytest
from opaca.domain.models import AuthorityResult
from opaca.wheel.agent import (
    AgentStatus,
    RepairFeedback,
    UntrustedIntentError,
    WheelAgentEvaluation,
    WheelAgentResult,
    run_wheel_decision,
)
from opaca.wheel.models import WheelAction

BASE_INTENT: dict[str, object] = {
    "action": WheelAction.SELL_CASH_SECURED_PUT.value,
    "underlying": "SPY",
    "market_view": "range-bound",
    "thesis": "defined downside entry",
    "willing_to_own_at_or_below": "746",
    "dte_preference": 5,
    "confidence": "0.70",
}


class FakeIntentProvider:
    def __init__(self, payloads: list[object]) -> None:
        self.payloads = payloads
        self.calls: list[dict[str, object]] = []

    def propose(
        self,
        *,
        wheel_decision_run_id: str,
        attempt_number: int,
        repair_feedback: tuple[RepairFeedback, ...],
    ) -> object:
        self.calls.append(
            {
                "run_id": wheel_decision_run_id,
                "attempt": attempt_number,
                "repair_feedback": repair_feedback,
            }
        )
        return self.payloads[len(self.calls) - 1]


def evaluate_compliant(_intent: object) -> WheelAgentEvaluation:
    return WheelAgentEvaluation(
        authority_result=AuthorityResult.AUTO,
        repair_feedback=(),
    )


def evaluate_rejected(_intent: object) -> WheelAgentEvaluation:
    return WheelAgentEvaluation(
        authority_result=AuthorityResult.REJECT,
        repair_feedback=(
            RepairFeedback(
                check_id="CHECK-20",
                facts=(
                    ("proposed_assignment_capital", "32000"),
                    ("hard_per_name_limit", "25000"),
                ),
            ),
        ),
    )


def run(
    provider: FakeIntentProvider,
    evaluate: Any,
    *,
    execute: Any = None,
    exposed_tools: frozenset[str] | None = None,
) -> WheelAgentResult:
    return run_wheel_decision(
        provider,
        wheel_decision_run_id="run-11",
        evaluate_intent=evaluate,
        execute=execute,
        exposed_mcp_tools=exposed_tools,
    )


def test_forbidden_authoritative_fields_are_rejected_not_ignored() -> None:
    provider = FakeIntentProvider([{**BASE_INTENT, "occ_symbol": "SPY260903P00746000"}])

    with pytest.raises(UntrustedIntentError):
        run(provider, evaluate_compliant)


def test_mcp_surface_failure_prevents_provider_invocation() -> None:
    provider = FakeIntentProvider([BASE_INTENT])

    result = run(
        provider,
        evaluate_compliant,
        exposed_tools=frozenset({"mcp__alpaca_readonly__get_clock"}),
    )

    assert result.status is AgentStatus.BLOCKED
    assert provider.calls == []


def test_rejected_run_allows_exactly_one_bounded_repair_then_stops() -> None:
    provider = FakeIntentProvider([BASE_INTENT, BASE_INTENT])
    execution_calls: list[object] = []

    result = run(provider, evaluate_rejected, execute=execution_calls.append)

    assert result.status is AgentStatus.NO_COMPLIANT_TRADE
    assert len(provider.calls) == 2
    assert execution_calls == []
    assert provider.calls[1]["attempt"] == 2
    feedback = cast(tuple[RepairFeedback, ...], provider.calls[1]["repair_feedback"])
    assert feedback[0].check_id == "CHECK-20"
    assert feedback[0].facts == (
        ("proposed_assignment_capital", "32000"),
        ("hard_per_name_limit", "25000"),
    )


def test_compliant_second_attempt_reaches_only_injected_downstream_callback() -> None:
    provider = FakeIntentProvider([BASE_INTENT, BASE_INTENT])
    execution_calls: list[object] = []
    evaluation_calls = 0

    def reject_then_accept(intent: object) -> WheelAgentEvaluation:
        nonlocal evaluation_calls
        evaluation_calls += 1
        if evaluation_calls == 1:
            return evaluate_rejected(intent)
        return evaluate_compliant(intent)

    result = run(provider, reject_then_accept, execute=execution_calls.append)

    assert result.status is AgentStatus.COMPLIANT
    assert len(provider.calls) == 2
    assert len(execution_calls) == 1


def test_repair_feedback_does_not_forward_secrets_or_mutation_capability() -> None:
    provider = FakeIntentProvider([BASE_INTENT, BASE_INTENT])

    result = run(provider, evaluate_rejected)

    assert result.status is AgentStatus.NO_COMPLIANT_TRADE
    serialized = repr(cast(tuple[RepairFeedback, ...], provider.calls[1]["repair_feedback"]))
    assert "a1facbe1522d" not in serialized
    assert "APCA_API_SECRET" not in serialized
    assert "submit_order" not in serialized


def test_agent_source_has_no_concrete_broker_mutation_capability() -> None:
    source_path = Path(inspect.getfile(run_wheel_decision))
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    forbidden = {"TradingClient", "submit_order", "PaperAlpacaOptionExecutionGateway"}
    names = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    } | {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }
    assert names.isdisjoint(forbidden)
