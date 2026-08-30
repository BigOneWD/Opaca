"""Explicit execution state machine. Illegal transitions fail closed."""

from __future__ import annotations

from opaca.broker.adapters import ALPACA_ORDER_STATUS_MAP
from opaca.domain.models import OrderState
from opaca.execution.errors import IllegalTransitionError
from opaca.persistence.types import ExecutionState

TERMINAL_STATES: frozenset[ExecutionState] = frozenset(
    {
        ExecutionState.FILLED,
        ExecutionState.CANCELLED,
        ExecutionState.REJECTED,
        ExecutionState.NOT_SUBMITTED,
    }
)

OPEN_RECOVERY_STATES: frozenset[ExecutionState] = frozenset(
    {
        ExecutionState.SUBMITTING,
        ExecutionState.SUBMITTED,
        ExecutionState.PARTIALLY_FILLED,
        ExecutionState.CANCEL_PENDING,
        ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION,
    }
)

LEGAL_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.READY: frozenset({ExecutionState.SUBMITTING}),
    ExecutionState.SUBMITTING: frozenset(
        {
            ExecutionState.SUBMITTED,
            ExecutionState.PARTIALLY_FILLED,
            ExecutionState.FILLED,
            ExecutionState.REJECTED,
            ExecutionState.NOT_SUBMITTED,
            ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION,
        }
    ),
    ExecutionState.SUBMITTED: frozenset(
        {
            ExecutionState.PARTIALLY_FILLED,
            ExecutionState.FILLED,
            ExecutionState.REJECTED,
            ExecutionState.CANCEL_PENDING,
            ExecutionState.CANCELLED,
            ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION,
        }
    ),
    ExecutionState.PARTIALLY_FILLED: frozenset(
        {
            ExecutionState.FILLED,
            ExecutionState.CANCEL_PENDING,
            ExecutionState.CANCELLED,
            ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION,
        }
    ),
    ExecutionState.CANCEL_PENDING: frozenset(
        {
            ExecutionState.CANCELLED,
            ExecutionState.FILLED,
            ExecutionState.PARTIALLY_FILLED,
            ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION,
        }
    ),
    ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION: frozenset(
        {
            ExecutionState.SUBMITTED,
            ExecutionState.PARTIALLY_FILLED,
            ExecutionState.FILLED,
            ExecutionState.REJECTED,
            ExecutionState.CANCELLED,
            ExecutionState.CANCEL_PENDING,
        }
    ),
    ExecutionState.FILLED: frozenset(),
    ExecutionState.CANCELLED: frozenset(),
    ExecutionState.REJECTED: frozenset(),
    ExecutionState.NOT_SUBMITTED: frozenset(),
}

_ALPACA_TO_EXECUTION: dict[OrderState, ExecutionState] = {
    OrderState.NEW: ExecutionState.SUBMITTED,
    OrderState.ACCEPTED: ExecutionState.SUBMITTED,
    OrderState.SUBMITTED: ExecutionState.SUBMITTED,
    OrderState.PARTIALLY_FILLED: ExecutionState.PARTIALLY_FILLED,
    OrderState.FILLED: ExecutionState.FILLED,
    OrderState.CANCELED: ExecutionState.CANCELLED,
    OrderState.CANCELED_REMAINDER: ExecutionState.CANCELLED,
    OrderState.REJECTED: ExecutionState.REJECTED,
    OrderState.EXPIRED: ExecutionState.CANCELLED,
    OrderState.UNKNOWN: ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION,
    OrderState.UNKNOWN_REQUIRES_REVIEW: ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION,
}


def validate_transition(current: ExecutionState, target: ExecutionState) -> None:
    if current is target:
        return
    allowed = LEGAL_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise IllegalTransitionError(
            f"illegal execution transition {current.value} → {target.value}"
        )


def map_broker_status(
    alpaca_status: str, *, filled_quantity: object | None = None
) -> ExecutionState:
    del filled_quantity
    status = alpaca_status.lower()
    if status == "pending_cancel":
        return ExecutionState.CANCEL_PENDING
    mapped = ALPACA_ORDER_STATUS_MAP.get(status)
    if mapped is None:
        return ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION
    return _ALPACA_TO_EXECUTION.get(mapped, ExecutionState.UNKNOWN_REQUIRES_RECONCILIATION)
