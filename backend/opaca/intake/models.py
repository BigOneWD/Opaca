"""Typed models for the untrusted AI obligation-intake boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from opaca.domain.models import Obligation


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
    effective_obligations: tuple[Obligation, ...]
    uncertain_reserved_amount: Decimal
    trade_blocked: bool
    block_reasons: tuple[str, ...]
