"""Deterministic logical-order identity (SPEC s9 CHECK-09).

client_order_id = hash(proposal_id + leg_index), encoded to satisfy the
actual Alpaca constraints verified in Phase -1B (B3): alphanumeric/hyphen
charset, max length 128; the 38-character encoding below passes with margin.
Retrying the same logical leg always produces the same broker identifier.
"""

from __future__ import annotations

import hashlib
import re

CLIENT_ORDER_ID_PREFIX = "opaca-"
CLIENT_ORDER_ID_MAX_LENGTH = 128
_HASH_HEX_LENGTH = 32

_CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,128}\Z")


def deterministic_client_order_id(proposal_id: str, leg_index: int) -> str:
    if not proposal_id:
        raise ValueError("proposal_id must be non-empty")
    if leg_index < 0:
        raise ValueError("leg_index must be >= 0")
    digest = hashlib.sha256(f"{proposal_id}:{leg_index}".encode()).hexdigest()
    return f"{CLIENT_ORDER_ID_PREFIX}{digest[:_HASH_HEX_LENGTH]}"


def is_valid_client_order_id(value: str) -> bool:
    """Alpaca constraints as verified in Phase -1B: charset and length."""
    return (
        bool(value)
        and len(value) <= CLIENT_ORDER_ID_MAX_LENGTH
        and bool(_CLIENT_ORDER_ID_RE.match(value))
    )
