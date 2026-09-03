"""RED-phase contracts for sanitized Wheel evidence and readiness."""

from __future__ import annotations

import json

import pytest
from opaca.wheel.evidence import (
    EvidenceSanitizationError,
    build_readiness,
    render_evidence,
    sanitize_wheel_evidence,
)

SAFE_EVIDENCE = {
    "mode": "DRY_RUN / SIMULATED",
    "paper": True,
    "underlying": "SPY",
    "occ_symbol": "SPY260903P00746000",
    "action": "SELL_CASH_SECURED_PUT",
    "contracts": 1,
    "strike": "746",
    "multiplier": "100",
    "assignment_capital": "74600",
    "authority_result": "AUTO",
    "client_order_id": "wheel-abc123",
    "broker_order_status": "NOT_SUBMITTED",
    "wheel_state": "CASH",
    "reserved_assignment_capital": "0",
    "reconciliation_result": "UNKNOWN",
    "account_fingerprint": "a1facbe1522d",
    "feed": "indicative",
    "software_ready": True,
    "mutation_ready": False,
    "blocker": "authoritative OPRA pricing unavailable",
}


def test_sanitizer_preserves_safe_fields_and_renders_json() -> None:
    sanitized = sanitize_wheel_evidence(SAFE_EVIDENCE)
    rendered = render_evidence(sanitized)

    assert sanitized == SAFE_EVIDENCE
    assert json.loads(rendered) == SAFE_EVIDENCE
    assert "a1facbe1522d" in rendered


@pytest.mark.parametrize(
    "field",
    [
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "ALPACA_SECRET_KEY",
        "bearer_token",
        "authorization_header",
        "account_id",
        "account_number",
        "env_contents",
    ],
)
def test_secret_and_raw_account_fields_fail_closed(field: str) -> None:
    with pytest.raises(EvidenceSanitizationError):
        sanitize_wheel_evidence({**SAFE_EVIDENCE, field: "secret-looking-value"})


def test_nested_secret_semantics_are_not_hidden_by_allowlisted_output() -> None:
    with pytest.raises(EvidenceSanitizationError):
        sanitize_wheel_evidence(
            {
                **SAFE_EVIDENCE,
                "diagnostics": {"api_secret": "do-not-leak"},
            }
        )


def test_indicative_feed_cannot_be_reported_as_mutation_ready() -> None:
    readiness = build_readiness(
        software_ready=True,
        paper=True,
        feed="indicative",
        opra_available=False,
    )

    assert readiness["software_ready"] is True
    assert readiness["mutation_ready"] is False
    assert readiness["blocker"] == "authoritative OPRA pricing unavailable"


def test_authoritative_feed_is_required_for_mutation_ready() -> None:
    readiness = build_readiness(
        software_ready=True,
        paper=True,
        feed="opra",
        opra_available=True,
    )

    assert readiness["mutation_ready"] is True
