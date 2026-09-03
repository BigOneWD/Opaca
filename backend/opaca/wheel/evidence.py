"""Sanitized Wheel evidence and operational readiness helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import Enum


class EvidenceSanitizationError(ValueError):
    """Evidence contained an unknown or sensitive field."""


_ALLOWED_FIELDS = frozenset(
    {
        "mode",
        "paper",
        "underlying",
        "occ_symbol",
        "action",
        "contracts",
        "strike",
        "multiplier",
        "assignment_capital",
        "authority_result",
        "client_order_id",
        "broker_order_id",
        "broker_order_status",
        "wheel_state",
        "reserved_assignment_capital",
        "reconciliation_result",
        "account_fingerprint",
        "feed",
        "software_ready",
        "paper_mutation_ready",
        "mutation_ready",
        "production_grade_market_data",
        "blocker",
        "dry_run",
    }
)
_SENSITIVE_FIELD_MARKERS = frozenset(
    {
        "secret",
        "password",
        "token",
        "credential",
        "authorization",
        "api_key",
        "account_id",
        "account_number",
        "env",
    }
)
_FINGERPRINT_RE = re.compile(r"^[0-9a-fA-F]{12,64}$")


def _sensitive_field(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    return any(marker in normalized for marker in _SENSITIVE_FIELD_MARKERS)


def _scan_sensitive_fields(value: object, path: str = "evidence") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise EvidenceSanitizationError(f"{path} contains a non-text field name")
            if _sensitive_field(key):
                raise EvidenceSanitizationError(f"sensitive evidence field rejected: {path}.{key}")
            _scan_sensitive_fields(child, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _scan_sensitive_fields(child, f"{path}[{index}]")


def sanitize_wheel_evidence(raw: Mapping[str, object]) -> dict[str, object]:
    """Return only the explicit evidence schema, rejecting secrets by semantics."""
    if not isinstance(raw, Mapping):
        raise EvidenceSanitizationError("Wheel evidence must be a mapping")
    _scan_sensitive_fields(raw)
    unknown = set(raw) - _ALLOWED_FIELDS
    if unknown:
        raise EvidenceSanitizationError(f"unknown evidence fields: {sorted(unknown)}")
    result = dict(raw)
    fingerprint = result.get("account_fingerprint")
    if fingerprint is not None:
        if not isinstance(fingerprint, str) or _FINGERPRINT_RE.fullmatch(fingerprint) is None:
            raise EvidenceSanitizationError("account_fingerprint must be a sanitized hex digest")
        result["account_fingerprint"] = fingerprint.lower()
    return result


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(child) for child in value]
    return value


def render_evidence(evidence: Mapping[str, object]) -> str:
    """Serialize already-produced evidence only after applying the same sanitizer."""
    safe = sanitize_wheel_evidence(evidence)
    return json.dumps(_json_value(safe), sort_keys=True, indent=2) + "\n"


def build_readiness(
    *,
    software_ready: bool,
    paper: bool,
    feed: str,
    opra_available: bool,
    indicative_available: bool = False,
) -> dict[str, object]:
    """Separate competition-PAPER readiness from production-grade data quality."""
    normalized_feed = feed.strip().lower() if isinstance(feed, str) else ""
    feed_available = {
        "opra": opra_available,
        "indicative": indicative_available,
    }.get(normalized_feed, False)
    paper_mutation_ready = software_ready and paper and feed_available
    production_grade_market_data = normalized_feed == "opra" and opra_available
    if not paper:
        blocker = "verified PAPER environment unavailable"
    elif not software_ready:
        blocker = "software readiness checks failed"
    elif normalized_feed not in {"opra", "indicative"} or not feed_available:
        blocker = "authoritative OPRA pricing unavailable"
    elif normalized_feed == "indicative":
        blocker = (
            "Alpaca INDICATIVE feed accepted for competition PAPER execution; "
            "not presented as OPRA or production-grade market data"
        )
    else:
        blocker = None
    return {
        "software_ready": software_ready,
        "paper_mutation_ready": paper_mutation_ready,
        # Keep the original field as a compatibility alias for existing callers.
        "mutation_ready": paper_mutation_ready,
        "production_grade_market_data": production_grade_market_data,
        "blocker": blocker,
        "feed": feed,
        "paper": paper,
    }


__all__ = [
    "EvidenceSanitizationError",
    "build_readiness",
    "render_evidence",
    "sanitize_wheel_evidence",
]
