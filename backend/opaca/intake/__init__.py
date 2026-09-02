"""Public API for fail-closed AI obligation intake."""

from opaca.intake.models import (
    Certainty,
    IntakeBlockedError,
    ObligationIntakeResult,
    ValidatedCandidate,
)
from opaca.intake.validation import (
    confirm_intake_completeness,
    parse_and_validate_extraction,
    require_effective_obligations,
)

__all__ = [
    "Certainty",
    "IntakeBlockedError",
    "ObligationIntakeResult",
    "ValidatedCandidate",
    "confirm_intake_completeness",
    "parse_and_validate_extraction",
    "require_effective_obligations",
]
