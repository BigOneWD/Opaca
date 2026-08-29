"""P1-a retest: PolicyContext.prices boundary.

At 5d33a05 a zero price silently defeated CHECK-04 and reached AUTO; a
negative price made CHECK-04 vacuous; malformed prices escaped evaluate()
as raw MoneyError/TypeError.
"""
from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest
from helpers import (  # type: ignore[import-not-found]
    DEFAULT_PRICES,
    decide,
    evaluate,
    make_context,
    make_order,
    make_proposal,
)
from opaca.domain.models import AuthorityResult, CheckId, Position, Side
from opaca.domain.money import MoneyError

SGOV = "SGOV"


def _pos(symbol: str, qty: str, price: str) -> Position:
    q = Decimal(qty)
    return Position(
        symbol=symbol,
        quantity=q,
        quantity_available=q,
        market_value=q * Decimal(price),
    )


def _prices(**over: object) -> dict[str, object]:
    p: dict[str, object] = dict(DEFAULT_PRICES)
    p.update(over)
    return p


# --- the exploit that reached AUTO at 5d33a05 -------------------------------


def test_zero_price_is_rejected_at_the_context_boundary() -> None:
    with pytest.raises(MoneyError):
        make_context(prices=_prices(SGOV=Decimal("0")))


def test_zero_price_concentration_bypass_is_unreachable() -> None:
    """The 5d33a05 exploit: 100k single-symbol buy vs a 100k pool passes
    CHECK-04 and reaches AUTO when the price is zeroed."""
    with pytest.raises(MoneyError):
        make_context(cash="100000", prices=_prices(SGOV=Decimal("0")))


def test_honest_price_still_fails_concentration_and_never_reaches_auto() -> None:
    """Teeth: with an honest price the control still bites."""
    ctx = make_context(cash="100000")
    order = make_order("p", 0, SGOV, Side.BUY, Decimal("900"), Decimal("100.69"))
    decision = evaluate(make_proposal("p", [order]), ctx)
    assert decision.passed is False
    c04 = next(r for r in decision.results if r.check_id is CheckId.CHECK_04)
    assert c04.passed is False
    assert decide(make_proposal("p", [order]), ctx).result is not AuthorityResult.AUTO


def test_negative_price_is_rejected() -> None:
    with pytest.raises(MoneyError):
        make_context(prices=_prices(SGOV=Decimal("-100")))


def test_negative_price_cannot_make_check04_vacuous() -> None:
    """5d33a05: a held 95k position + price -100 collapsed the pool base and
    CHECK-04 returned 'vacuous' PASS from a 95%-concentrated portfolio."""
    with pytest.raises(MoneyError):
        make_context(
            cash="5000",
            positions=[_pos(SGOV, "943", "100.69")],
            prices=_prices(SGOV=Decimal("-100")),
        )


# --- malformed inputs no longer escape as raw TypeError ---------------------


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(100.69, id="float"),
        pytest.param(True, id="bool"),
        pytest.param(None, id="none"),
        pytest.param("100.69", id="str"),
        pytest.param(10069, id="int"),
        pytest.param(Decimal("NaN"), id="nan"),
        pytest.param(Decimal("sNaN"), id="snan"),
        pytest.param(Decimal("Infinity"), id="inf"),
        pytest.param(Decimal("-Infinity"), id="neg-inf"),
        pytest.param(Decimal("1e26"), id="at-magnitude-limit"),
        pytest.param(Decimal("1e30"), id="oversized"),
        pytest.param(Decimal("-0"), id="negative-zero"),
        pytest.param(Decimal("0.00"), id="zero-with-scale"),
    ],
)
def test_every_malformed_price_raises_moneyerror_not_typeerror(bad: object) -> None:
    with pytest.raises(MoneyError):
        make_context(prices=_prices(SGOV=bad))


def test_moneyerror_is_a_valueerror() -> None:
    assert issubclass(MoneyError, ValueError)


def test_bad_price_on_an_unrelated_symbol_is_also_rejected() -> None:
    """Validation is whole-mapping, not just the traded symbol."""
    with pytest.raises(MoneyError):
        make_context(prices=_prices(ZZZZ=Decimal("0")))


# --- the validated mapping cannot be tampered with afterwards ---------------


def test_prices_mapping_is_read_only_after_construction() -> None:
    ctx = make_context()
    with pytest.raises(TypeError):
        ctx.prices[SGOV] = Decimal("0")  # type: ignore[index]


def test_mutating_the_caller_dict_does_not_change_the_context() -> None:
    src = dict(DEFAULT_PRICES)
    ctx = make_context(prices=src)
    src[SGOV] = Decimal("0")
    assert ctx.prices[SGOV] == Decimal("100.69")


def test_dataclasses_replace_revalidates() -> None:
    ctx = make_context()
    with pytest.raises(MoneyError):
        dataclasses.replace(ctx, prices=_prices(SGOV=Decimal("0")))


def test_valid_prices_survive_unchanged() -> None:
    ctx = make_context()
    assert dict(ctx.prices) == dict(DEFAULT_PRICES)
    assert all(isinstance(v, Decimal) for v in ctx.prices.values())


# --- missing price must still fail closed (unchanged behaviour) -------------


def test_missing_price_on_a_held_symbol_still_fails_closed() -> None:
    ctx = make_context(
        cash="100000",
        positions=[_pos("SHV", "10", "110.00")],
        prices={SGOV: Decimal("100.69"), "BIL": Decimal("92.00")},
    )
    order = make_order("p", 0, SGOV, Side.BUY, Decimal("1"), Decimal("100.69"))
    decision = evaluate(make_proposal("p", [order]), ctx)
    c04 = next(r for r in decision.results if r.check_id is CheckId.CHECK_04)
    c11 = next(r for r in decision.results if r.check_id is CheckId.CHECK_11)
    assert c04.passed is False
    assert c11.passed is False
    assert decision.passed is False
