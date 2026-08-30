"""Architectural guard: read-only path has no broker mutation surface.

Paper ``submit_order`` / ``cancel_order_by_id`` exist only on the separate
execution gateway (``opaca.broker.paper_execution`` / ``opaca.execution``).
"""

from __future__ import annotations

FORBIDDEN_BROKER_MUTATIONS: frozenset[str] = frozenset(
    {
        "submit_order",
        "cancel_order",
        "cancel_order_by_id",
        "cancel_orders",
        "replace_order",
        "replace_order_by_id",
        "close_position",
        "close_all_positions",
        "exercise_options_position",
        "post",
        "put",
        "patch",
        "delete",
        "request",
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


def nested_mutable_client_method(gateway: object) -> str | None:
    """Return a forbidden method name if a nested TradingClient-like object is retained.

    Second arguments to getattr are string constants so this is not a computed
    dispatcher onto a broker object.
    """
    nested = getattr(gateway, "_client", None)
    if nested is None:
        nested = getattr(gateway, "client", None)
    if nested is None:
        nested = getattr(gateway, "_trading_client", None)
    if nested is None:
        return None
    if callable(getattr(nested, "submit_order", None)):
        return "submit_order"
    if callable(getattr(nested, "cancel_order", None)):
        return "cancel_order"
    if callable(getattr(nested, "cancel_order_by_id", None)):
        return "cancel_order_by_id"
    if callable(getattr(nested, "cancel_orders", None)):
        return "cancel_orders"
    if callable(getattr(nested, "replace_order", None)):
        return "replace_order"
    if callable(getattr(nested, "replace_order_by_id", None)):
        return "replace_order_by_id"
    if callable(getattr(nested, "close_position", None)):
        return "close_position"
    if callable(getattr(nested, "close_all_positions", None)):
        return "close_all_positions"
    if callable(getattr(nested, "exercise_options_position", None)):
        return "exercise_options_position"
    if callable(getattr(nested, "post", None)):
        return "post"
    if callable(getattr(nested, "put", None)):
        return "put"
    if callable(getattr(nested, "patch", None)):
        return "patch"
    if callable(getattr(nested, "delete", None)):
        return "delete"
    if callable(getattr(nested, "request", None)):
        return "request"
    return None
