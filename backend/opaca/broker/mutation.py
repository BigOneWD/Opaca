"""Architectural guard: this phase has no broker mutation surface."""

from __future__ import annotations

FORBIDDEN_BROKER_MUTATIONS: frozenset[str] = frozenset(
    {
        "submit_order",
        "cancel_order",
        "replace_order",
        "close_position",
        "close_all_positions",
        "exercise_options_position",
        "cancel_orders",
    }
)

ALLOWED_GATEWAY_METHODS: frozenset[str] = frozenset(
    {
        "get_account",
        "get_positions",
        "get_asset",
        "get_open_orders",
        "get_order_by_client_id",
        "get_calendar",
        "get_clock",
    }
)
