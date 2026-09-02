"""Typed models for the untrusted AI obligation-intake boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from opaca.domain.models import Obligation


MAX_DOCUMENT_CHARS = 50_000
MAX_MODEL_RESPONSE_CHARS = 100_000
MAX_PROVIDER_RESPONSE_BYTES = 400_000
MAX_DOCUMENT_SUMMARY_CHARS = 2_000
MAX_CANDIDATE_NAME_CHARS = 500
MAX_UNCERTAINTY_REASON_CHARS = 2_000
MAX_SOURCE_EXCERPT_CHARS = 10_000


class IntakeBlockedError(RuntimeError):
    """Raised when intake output is unsafe for downstream treasury use."""


class Certainty(StrEnum):
    CONFIRMED = "CONFIRMED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class ValidatedCandidate:
    candidate_id: str
    name: str
    amount: Decimal | None
    stated_due_date: date | None
    effective_due_date: date | None
    certainty: Certainty
    uncertainty_reason: str | None
    source_excerpt: str
    source_sha256: str
    reserved_conservatively: bool


@dataclass(frozen=True)
class ObligationIntakeResult:
    source_sha256: str
    candidates: tuple[ValidatedCandidate, ...]
    _effective_obligations: tuple[Obligation, ...]
    uncertain_reserved_amount: Decimal
    trade_blocked: bool
    block_reasons: tuple[str, ...]

    @property
    def effective_obligations(self) -> tuple[Obligation, ...]:
        if self.trade_blocked:
            reasons = ", ".join(self.block_reasons)
            raise IntakeBlockedError(f"intake blocked: {reasons}")
        return self._effective_obligations
