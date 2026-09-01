"""Public API for fail-closed AI obligation intake."""

from opaca.intake.models import Certainty, ObligationIntakeResult, ValidatedCandidate
from opaca.intake.validation import (
    IntakeBlockedError,
    parse_and_validate_extraction,
    require_effective_obligations,
)

__all__ = [
    "Certainty",
    "IntakeBlockedError",
    "ObligationIntakeResult",
    "ValidatedCandidate",
    "parse_and_validate_extraction",
    "require_effective_obligations",
]
