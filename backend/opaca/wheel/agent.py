"""Bounded, read-only intent orchestration for the Competition Wheel."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol

from opaca.domain.models import AuthorityResult
from opaca.wheel.mcp_guard import (
    MCP_ALLOWED_TOOLS,
    MCP_REQUIRED_READ_TOOLS,
    McpToolSurfaceError,
    assert_mcp_tool_surface,
)
from opaca.wheel.models import OptionIntent, WheelAction

MAX_AGENT_ATTEMPTS_PER_RUN = 2
_INTENT_FIELDS = frozenset(
    {
        "action",
        "underlying",
        "market_view",
        "thesis",
        "willing_to_own_at_or_below",
        "dte_preference",
        "confidence",
    }
)
_FEEDBACK_FACTS = frozenset(
    {
        "proposed_assignment_capital",
        "hard_per_name_limit",
        "hard_aggregate_limit",
        "permitted_bounds",
        "reason_code",
    }
)


class UntrustedIntentError(ValueError):
    """Provider output attempted to cross the deterministic authority boundary."""


class AgentStatus(StrEnum):
    BLOCKED = "BLOCKED"
    COMPLIANT = "COMPLIANT"
    NO_COMPLIANT_TRADE = "NO_COMPLIANT_TRADE"


@dataclass(frozen=True)
class RepairFeedback:
    """A bounded, non-secret fact set suitable for one repair prompt."""

    check_id: str
    facts: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if len(self.check_id) != 8 or not self.check_id.startswith("CHECK-"):
            raise ValueError("repair feedback must identify a CHECK-XX code")
        if not self.check_id[6:].isdigit():
            raise ValueError("repair feedback must identify a CHECK-XX code")
        normalized: list[tuple[str, str]] = []
        for key, value in self.facts:
            if key not in _FEEDBACK_FACTS or not isinstance(value, str):
                raise ValueError("repair feedback contains an unauthorized fact")
            if not value or len(value) > 64 or any(ord(char) < 32 for char in value):
                raise ValueError("repair feedback fact is malformed")
            if key != "reason_code":
                try:
                    Decimal(value)
                except InvalidOperation as exc:
                    raise ValueError("repair feedback arithmetic is not numeric") from exc
            normalized.append((key, value))
        object.__setattr__(self, "facts", tuple(normalized))


@dataclass(frozen=True)
class WheelAgentEvaluation:
    """Deterministic downstream adjudication of one untrusted intent."""

    authority_result: AuthorityResult
    repair_feedback: tuple[RepairFeedback, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "repair_feedback", tuple(self.repair_feedback))


@dataclass(frozen=True)
class WheelAgentResult:
    status: AgentStatus
    attempts: int
    reasons: tuple[str, ...]
    intent: OptionIntent | None = None
    downstream_result: object | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))


class WheelIntentProvider(Protocol):
    """Provider receives no broker client and can only return intent fields."""

    def propose(
        self,
        *,
        wheel_decision_run_id: str,
        attempt_number: int,
        repair_feedback: tuple[RepairFeedback, ...],
    ) -> object:
        """Return an untrusted OptionIntent payload."""


def _parse_intent(payload: object) -> OptionIntent:
    if isinstance(payload, OptionIntent):
        return payload
    if not isinstance(payload, Mapping):
        raise UntrustedIntentError("provider payload must be an intent object")
    keys = frozenset(payload)
    forbidden = keys - _INTENT_FIELDS
    missing = _INTENT_FIELDS - keys
    if forbidden:
        raise UntrustedIntentError(
            f"provider payload contains authoritative fields: {sorted(forbidden)}"
        )
    if missing:
        raise UntrustedIntentError(f"provider payload is missing intent fields: {sorted(missing)}")
    try:
        return OptionIntent(
            action=WheelAction(str(payload["action"])),
            underlying=payload["underlying"],
            market_view=payload["market_view"],
            thesis=payload["thesis"],
            willing_to_own_at_or_below=payload["willing_to_own_at_or_below"],
            dte_preference=payload["dte_preference"],
            confidence=payload["confidence"],
        )
    except (TypeError, ValueError) as exc:
        raise UntrustedIntentError("provider payload is not a valid OptionIntent") from exc


def _blocked(reason: str, attempts: int = 0) -> WheelAgentResult:
    return WheelAgentResult(status=AgentStatus.BLOCKED, attempts=attempts, reasons=(reason,))


def run_wheel_decision(
    provider: WheelIntentProvider,
    *,
    wheel_decision_run_id: str,
    evaluate_intent: Callable[[OptionIntent], WheelAgentEvaluation],
    execute: Callable[[OptionIntent], object] | None = None,
    exposed_mcp_tools: Iterable[str] | None = None,
    required_mcp_tools: Iterable[str] = MCP_REQUIRED_READ_TOOLS,
) -> WheelAgentResult:
    """Guard MCP, obtain at most two intents, and delegate compliant output."""
    exposed = MCP_ALLOWED_TOOLS if exposed_mcp_tools is None else exposed_mcp_tools
    try:
        assert_mcp_tool_surface(exposed, MCP_ALLOWED_TOOLS, required_mcp_tools)
    except McpToolSurfaceError as exc:
        return _blocked(str(exc))

    feedback: tuple[RepairFeedback, ...] = ()
    for attempt in range(1, MAX_AGENT_ATTEMPTS_PER_RUN + 1):
        payload = provider.propose(
            wheel_decision_run_id=wheel_decision_run_id,
            attempt_number=attempt,
            repair_feedback=feedback,
        )
        intent = _parse_intent(payload)
        evaluation = evaluate_intent(intent)
        if evaluation.authority_result is not AuthorityResult.REJECT:
            downstream_result = None if execute is None else execute(intent)
            return WheelAgentResult(
                status=AgentStatus.COMPLIANT,
                attempts=attempt,
                reasons=("deterministic policy and authority accepted intent",),
                intent=intent,
                downstream_result=downstream_result,
            )
        feedback = evaluation.repair_feedback

    return WheelAgentResult(
        status=AgentStatus.NO_COMPLIANT_TRADE,
        attempts=MAX_AGENT_ATTEMPTS_PER_RUN,
        reasons=("bounded attempts exhausted without a compliant trade",),
    )


__all__ = [
    "AgentStatus",
    "MAX_AGENT_ATTEMPTS_PER_RUN",
    "RepairFeedback",
    "UntrustedIntentError",
    "WheelAgentEvaluation",
    "WheelAgentResult",
    "WheelIntentProvider",
    "run_wheel_decision",
]
