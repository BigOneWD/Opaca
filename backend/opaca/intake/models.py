"""Typed models for the untrusted AI obligation-intake boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from opaca.domain.models import Obligation


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
