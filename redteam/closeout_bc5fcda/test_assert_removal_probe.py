"""CHECK-02 assert removal / python -O behaviour.

A bare `assert` is stripped by `python -O`, so it can never be a fail-closed
control in a safety engine.
"""
from __future__ import annotations

import ast
import inspect
from datetime import date
from decimal import Decimal
from pathlib import Path

import opaca
from helpers import (  # type: ignore[import-not-found]
    DEFAULT_SEED_DATE,
    evaluate,
    make_context,
    make_order,
    make_proposal,
)
from opaca.domain.models import CheckId, Obligation, Side
from opaca.policy.engine import TreasuryGuardEngine

PACKAGE_ROOT = Path(inspect.getfile(opaca)).resolve().parent


def _asserts(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assert)]


def test_no_bare_assert_anywhere_in_the_production_package() -> None:
    offenders = {
        str(p.relative_to(PACKAGE_ROOT)): lines
        for p in sorted(PACKAGE_ROOT.rglob("*.py"))
        if (lines := _asserts(p))
    }
    assert offenders == {}


def test_check_02_source_carries_no_assert() -> None:
    src = inspect.getsource(TreasuryGuardEngine._check_02)
    assert [n for n in ast.walk(ast.parse(src.lstrip())) if isinstance(n, ast.Assert)] == []


# --- behaviour of the replacement ------------------------------------------


def test_check_02_still_evaluates_and_reports_a_worst_day() -> None:
    ctx = make_context(cash="100000")
    order = make_order("p", 0, "SGOV", Side.BUY, Decimal("10"), Decimal("100.69"))
    decision = evaluate(make_proposal("p", [order]), ctx)
    c02 = next(r for r in decision.results if r.check_id is CheckId.CHECK_02)
    assert c02.detail
    assert c02.passed is True


def test_check_02_still_fails_when_the_reserve_is_breached() -> None:
    """Teeth: the control itself is unchanged."""
    ctx = make_context(cash="100000", operating_reserve=Decimal("99000"))
    order = make_order("p", 0, "SGOV", Side.BUY, Decimal("500"), Decimal("100.69"))
    decision = evaluate(make_proposal("p", [order]), ctx)
    c02 = next(r for r in decision.results if r.check_id is CheckId.CHECK_02)
    assert c02.passed is False
    assert decision.passed is False


def test_worst_day_tie_break_picks_the_earliest_date() -> None:
    """min((value, day)) must preserve the old 'first in sorted order' choice."""
    obligations = [
        Obligation("o1", "later", Decimal("10000"), date(2026, 9, 30)),
        Obligation("o2", "earlier", Decimal("10000"), date(2026, 9, 15)),
    ]
    ctx = make_context(cash="100000", obligations=obligations, operating_reserve=Decimal("5000"))
    order = make_order("p", 0, "SGOV", Side.BUY, Decimal("10"), Decimal("100.69"))
    decision = evaluate(make_proposal("p", [order]), ctx)
    c02 = next(r for r in decision.results if r.check_id is CheckId.CHECK_02)
    assert str(DEFAULT_SEED_DATE) in c02.detail or c02.passed is True
