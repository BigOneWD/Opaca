"""Deterministic logical identity for Wheel option orders."""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal

from opaca.domain.money import non_negative_money
from opaca.wheel.models import WheelAction

WHEEL_CLIENT_ORDER_ID_PREFIX = "wheel-"
WHEEL_CLIENT_ORDER_ID_MAX_LENGTH = 128
_CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,128}\Z")


def wheel_client_order_id(
    *,
    wheel_decision_run_id: str,
    attempt_number: int,
    occ_symbol: str,
    action: WheelAction,
    contracts: int,
    limit_premium: Decimal,
) -> str:
    """Hash all fields that define one retryable logical Wheel order."""
    if not isinstance(wheel_decision_run_id, str) or not wheel_decision_run_id.strip():
        raise ValueError("wheel_decision_run_id must be non-empty")
    if isinstance(attempt_number, bool) or attempt_number < 1:
        raise ValueError("attempt_number must be >= 1")
    if not isinstance(occ_symbol, str) or not occ_symbol.strip():
        raise ValueError("occ_symbol must be non-empty")
    if not isinstance(action, WheelAction):
        raise ValueError("action must be a WheelAction")
    if isinstance(contracts, bool) or contracts < 1:
        raise ValueError("contracts must be >= 1")
    premium = non_negative_money(limit_premium)
    canonical = "|".join(
        (
            wheel_decision_run_id,
            str(attempt_number),
            occ_symbol,
            action.value,
            str(contracts),
            format(premium, "f"),
        )
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{WHEEL_CLIENT_ORDER_ID_PREFIX}{digest[:32]}"


def is_valid_wheel_client_order_id(value: str) -> bool:
    """Validate the broker-safe character and length envelope."""
    return (
        bool(value)
        and len(value) <= WHEEL_CLIENT_ORDER_ID_MAX_LENGTH
        and bool(_CLIENT_ORDER_ID_RE.fullmatch(value))
    )
