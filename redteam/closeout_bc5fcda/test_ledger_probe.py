"""P1-b retest: LedgerInconsistencyError must become a failed check, not an
exception escaping evaluate() / decide()."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from helpers import (  # type: ignore[import-not-found]
    DEFAULT_SEED_DATE,
    decide,
    evaluate,
    make_context,
    make_order,
    make_proposal,
)
from opaca.domain.models import (
    AuthorityResult,
    CheckId,
    Position,
    SettlementEvent,
    Side,
)
from opaca.treasury.liquidity import LedgerInconsistencyError, compute_liquidity

SGOV = "SGOV"
LEDGER_CHECKS = {
    CheckId.CHECK_01,
    CheckId.CHECK_02,
    CheckId.CHECK_04,
    CheckId.CHECK_06,
    CheckId.CHECK_11,
}


def _inconsistent_events(total: str = "200000") -> list[SettlementEvent]:
    """Unsettled proceeds larger than broker cash -> derived settled cash < 0."""
    return [
        SettlementEvent(
            event_id="e1",
            symbol=SGOV,
            trade_date=DEFAULT_SEED_DATE,
            settlement_date=date(2026, 9, 2),
            amount=Decimal(total),
        )
    ]


def _ctx(**kw: object):
    return make_context(cash="100000", settlement_events=_inconsistent_events(), **kw)  # type: ignore[arg-type]


def test_the_underlying_condition_still_raises_at_the_liquidity_layer() -> None:
    """Teeth: the domain-level guard is unchanged; only the engine catches it."""
    ctx = _ctx()
    with pytest.raises(LedgerInconsistencyError):
        compute_liquidity(
            broker=ctx.broker,
            obligations=ctx.obligations,
            settlement_events=ctx.settlement_events,
            operating_reserve=ctx.liquidity_policy.operating_reserve,
            as_of=ctx.execution.now.date(),
        )


def test_evaluate_returns_a_decision_instead_of_raising() -> None:
    ctx = _ctx()
    order = make_order("p", 0, SGOV, Side.BUY, Decimal("1"), Decimal("100.69"))
    decision = evaluate(make_proposal("p", [order]), ctx)
    assert decision.passed is False


def test_every_liquidity_dependent_check_fails_closed_with_a_ledger_reason() -> None:
    ctx = _ctx()
    order = make_order("p", 0, SGOV, Side.BUY, Decimal("1"), Decimal("100.69"))
    decision = evaluate(make_proposal("p", [order]), ctx)
    seen = {r.check_id: r for r in decision.results}
    for check_id in LEDGER_CHECKS:
        assert check_id in seen, check_id
        assert seen[check_id].passed is False, check_id
        assert "ledger inconsistent" in seen[check_id].detail, check_id


def test_decide_rejects_and_never_reaches_auto() -> None:
    ctx = _ctx()
    order = make_order("p", 0, SGOV, Side.BUY, Decimal("1"), Decimal("100.69"))
    result = decide(make_proposal("p", [order]), ctx)
    assert result.result is AuthorityResult.REJECT
    assert result.result is not AuthorityResult.AUTO


def test_sell_only_proposal_also_fails_closed() -> None:
    """A sell leg must not slip through on 'no buy notional' vacuity."""
    q = Decimal("100")
    ctx = _ctx(positions=[Position(SGOV, q, q, q * Decimal("100.69"))])
    order = make_order("p", 0, SGOV, Side.SELL, Decimal("10"), Decimal("100.69"))
    decision = evaluate(make_proposal("p", [order]), ctx)
    assert decision.passed is False
    c12 = next(r for r in decision.results if r.check_id is CheckId.CHECK_12)
    assert c12.passed is False
    assert "ledger inconsistent" in c12.detail
    assert decide(make_proposal("p", [order]), ctx).result is AuthorityResult.REJECT


def test_empty_proposal_also_fails_closed() -> None:
    decision = evaluate(make_proposal("p", []), _ctx())
    assert decision.passed is False


def test_boundary_exactly_zero_settled_cash_is_consistent() -> None:
    """100,000 cash vs exactly 100,000 unsettled is NOT inconsistent."""
    events = _inconsistent_events(total="100000")
    ctx = make_context(cash="100000", settlement_events=events, operating_reserve=Decimal("0"))
    order = make_order("p", 0, SGOV, Side.BUY, Decimal("1"), Decimal("100.69"))
    decision = evaluate(make_proposal("p", [order]), ctx)
    details = " ".join(r.detail for r in decision.results)
    assert "ledger inconsistent" not in details


def test_one_cent_over_is_inconsistent() -> None:
    events = _inconsistent_events(total="100000.01")
    ctx = make_context(cash="100000", settlement_events=events)
    order = make_order("p", 0, SGOV, Side.BUY, Decimal("1"), Decimal("100.69"))
    decision = evaluate(make_proposal("p", [order]), ctx)
    c01 = next(r for r in decision.results if r.check_id is CheckId.CHECK_01)
    assert c01.passed is False
    assert "ledger inconsistent" in c01.detail


def test_a_consistent_ledger_is_unaffected() -> None:
    """Regression: no ledger reason leaks into an ordinary evaluation."""
    ctx = make_context(cash="100000")
    order = make_order("p", 0, SGOV, Side.BUY, Decimal("10"), Decimal("100.69"))
    decision = evaluate(make_proposal("p", [order]), ctx)
    assert "ledger inconsistent" not in " ".join(r.detail for r in decision.results)
