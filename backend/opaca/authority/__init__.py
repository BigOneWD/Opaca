"""Authority engine package."""

from opaca.authority.engine import (
    ROLLING_COUNT_WINDOW,
    ROLLING_NOTIONAL_WINDOW,
    RUNAWAY_WINDOW,
    apply_human_approval,
    authority_dimension_violations,
    decide_authority,
    executions_in_window,
    rolling_count,
    rolling_notional,
    runaway_order_count_violation,
)

__all__ = [
    "RUNAWAY_WINDOW",
    "ROLLING_COUNT_WINDOW",
    "ROLLING_NOTIONAL_WINDOW",
    "apply_human_approval",
    "authority_dimension_violations",
    "decide_authority",
    "executions_in_window",
    "rolling_count",
    "rolling_notional",
    "runaway_order_count_violation",
]
