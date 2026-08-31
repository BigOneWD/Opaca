"""S4: canonical price binding. The $0.01 reference-price attack."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from opaca.domain.models import Side
from opaca.execution.service import execute_reserved_proposal
from opaca.market.binding import (
    BoundExecutionPrice,
    bind_buy,
    bind_single_leg_proposal,
    price_binding_failure,
    require_price_binding,
)
from opaca.market.errors import PriceBindingError
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.reconciliation.service import reconcile

from support import (
    DEFAULT_NOW,
    DEFAULT_PRICES,
    SGOV,
    leg,
    proposal_of,
    quotes_for,
    world,
)


def _reserve(w, proposal, prices, bindings=None, now=DEFAULT_NOW):
    recon = reconcile(w.store, w.read(), now=now)
    return evaluate_and_reserve(
        w.store, proposal, now=now, prices=prices,
        expected_snapshot_version=recon.snapshot.version, price_bindings=bindings)


def _active_cash(store):
    from opaca.persistence.types import ReservationKind, ReservationStatus
    return sum((r.amount for r in store.active_reservations()
                if r.kind is ReservationKind.CASH_DEPLOYMENT and r.amount is not None
                and r.status is ReservationStatus.ACTIVE), Decimal("0"))


# ------------------------------------------------- the original exploit


def test_the_one_cent_reference_price_is_blocked_at_reservation(tmp_path):
    w = world(tmp_path, qty="0", cash="500000")
    absurd = proposal_of("x1", [leg("x1", 0, "SGOV", Side.BUY, "100", "0.01")])
    out = _reserve(w, absurd, DEFAULT_PRICES)
    assert out.is_auto is False
    assert out.decision is None
    assert "binding mismatch" in (out.block_reason or "")
    assert _active_cash(w.store) == Decimal("0")
    w.close()


def test_the_one_cent_reference_price_is_blocked_again_at_execution(tmp_path):
    w = world(tmp_path, qty="0", cash="500000")
    honest = proposal_of("x2", [leg("x2", 0, "SGOV", Side.BUY, "1", SGOV)])
    out = _reserve(w, honest, DEFAULT_PRICES)
    assert out.is_auto is True
    # the attacker now hands execution a leg priced at a cent
    tampered = proposal_of("x2", [leg("x2", 0, "SGOV", Side.BUY, "1", "0.01")])
    mutate = w.mutate()
    result = execute_reserved_proposal(
        w.store, w.read(), mutate, tampered, now=DEFAULT_NOW, prices=DEFAULT_PRICES)
    assert mutate.submit_calls == 0
    assert result.blocked is True
    w.close()


def test_a_wrong_price_can_no_longer_flip_the_authority_decision(tmp_path):
    """300 SGOV is APPROVAL_REQUIRED at the true price; understating it is blocked,
    not silently promoted to AUTO."""
    honest_dir, wrong_dir = tmp_path / "h", tmp_path / "w"
    honest_dir.mkdir(); wrong_dir.mkdir()

    w1 = world(honest_dir, qty="0", cash="500000")
    honest = proposal_of("b1", [leg("b1", 0, "SGOV", Side.BUY, "300", SGOV)])
    out_h = _reserve(w1, honest, DEFAULT_PRICES)
    w1.close()

    w2 = world(wrong_dir, qty="0", cash="500000")
    understated = proposal_of("b1", [leg("b1", 0, "SGOV", Side.BUY, "300", "10.00")])
    out_w = _reserve(w2, understated, DEFAULT_PRICES)
    w2.close()

    assert out_h.is_auto is False
    assert out_w.is_auto is False, "an understated price must not reach AUTO"
    assert "binding mismatch" in (out_w.block_reason or "")


# ------------------------------------------------- binding object integrity


def test_a_binding_cannot_carry_a_valuation_other_than_the_quote():
    q = quotes_for()["SGOV"]
    with pytest.raises(PriceBindingError):
        BoundExecutionPrice(quote=q, valuation_price=Decimal("0.01"),
                            reference_price=Decimal("0.01"), limit_price=Decimal("0.01"),
                            side=Side.BUY, quantity=Decimal("1"),
                            tolerance=Decimal("0.001"), max_cash_obligation=Decimal("0.01"))


def test_a_buy_binding_cannot_understate_the_cash_obligation():
    q = quotes_for()["SGOV"]
    bound = bind_buy(q, Decimal("10"))
    with pytest.raises(PriceBindingError):
        replace(bound, max_cash_obligation=Decimal("1.00"))
    with pytest.raises(PriceBindingError):
        replace(bound, reference_price=q.price)          # below the LIMIT
    with pytest.raises(PriceBindingError):
        replace(bound, limit_price=Decimal("0.01"))


def test_a_binding_for_a_different_symbol_is_refused():
    quotes = quotes_for()
    bound = bind_buy(quotes["SGOV"], Decimal("1"))
    proposal = proposal_of("s1", [leg("s1", 0, "BIL", Side.BUY, "1", bound.limit_price)])
    reason = price_binding_failure(proposal, {"BIL": bound.valuation_price},
                                   bindings={"BIL": bound})
    assert reason is not None


def test_bindings_for_symbols_not_on_the_proposal_are_refused():
    quotes = quotes_for()
    bound = bind_buy(quotes["SGOV"], Decimal("1"))
    other = bind_buy(quotes["BIL"], Decimal("1"))
    proposal = proposal_of("s2", [leg("s2", 0, "SGOV", Side.BUY, "1", bound.limit_price)])
    reason = price_binding_failure(
        proposal, {"SGOV": bound.valuation_price, "BIL": other.valuation_price},
        bindings={"SGOV": bound, "BIL": other})
    assert reason is not None


def test_a_missing_policy_price_is_refused():
    proposal = proposal_of("s3", [leg("s3", 0, "SGOV", Side.BUY, "1", SGOV)])
    with pytest.raises(PriceBindingError):
        require_price_binding(proposal, {})


def test_a_quantity_change_after_binding_is_refused():
    quotes = quotes_for()
    bound = bind_buy(quotes["SGOV"], Decimal("1"))
    proposal = proposal_of("s4", [leg("s4", 0, "SGOV", Side.BUY, "5", bound.limit_price)])
    reason = price_binding_failure(proposal, {"SGOV": bound.valuation_price},
                                   bindings={"SGOV": bound})
    assert reason is not None


# ------------------------------------- price injected between reserve and order


def test_a_lower_price_injected_after_reservation_cannot_reach_the_broker(tmp_path):
    w = world(tmp_path, qty="0", cash="500000")
    quotes = quotes_for()
    bound = bind_buy(quotes["SGOV"], Decimal("1"))
    proposal, prices, bindings = bind_single_leg_proposal("inj", bound, quotes)
    out = _reserve(w, proposal, prices, bindings)
    assert out.is_auto is True
    reserved = _active_cash(w.store)
    assert reserved == bound.max_cash_obligation

    cheap = dict(prices)
    cheap["SGOV"] = Decimal("0.01")
    mutate = w.mutate()
    result = execute_reserved_proposal(
        w.store, w.read(), mutate, proposal, now=DEFAULT_NOW, prices=cheap,
        price_bindings=bindings)
    assert mutate.submit_calls == 0
    assert result.blocked is True
    assert _active_cash(w.store) == reserved
    w.close()


def test_a_cheaper_canonical_quote_swapped_in_at_execution_is_refused(tmp_path):
    w = world(tmp_path, qty="0", cash="500000")
    quotes = quotes_for()
    bound = bind_buy(quotes["SGOV"], Decimal("1"))
    proposal, prices, bindings = bind_single_leg_proposal("swap", bound, quotes)
    assert _reserve(w, proposal, prices, bindings).is_auto is True

    cheap_quotes = quotes_for(sgov="0.01")
    cheap_bound = bind_buy(cheap_quotes["SGOV"], Decimal("1"))
    mutate = w.mutate()
    result = execute_reserved_proposal(
        w.store, w.read(), mutate, proposal, now=DEFAULT_NOW,
        prices={"SGOV": Decimal("0.01")}, price_bindings={"SGOV": cheap_bound})
    assert mutate.submit_calls == 0
    assert result.blocked is True
    w.close()


def test_reserved_cash_is_bound_to_quantity_times_limit(tmp_path):
    w = world(tmp_path, qty="0", cash="500000")
    quotes = quotes_for()
    bound = bind_buy(quotes["SGOV"], Decimal("3"))
    proposal, prices, bindings = bind_single_leg_proposal("cash", bound, quotes)
    out = _reserve(w, proposal, prices, bindings)
    assert out.is_auto is True
    assert _active_cash(w.store) == Decimal("3") * bound.limit_price
    assert _active_cash(w.store) > Decimal("3") * quotes["SGOV"].price
    w.close()


# ------------------------------- the guard is opt-in: caller-supplied pair


def test_FINDING_a_caller_supplied_price_pair_still_satisfies_the_binding(tmp_path):
    """Without price_bindings the only requirement is prices[s] == reference_price.
    A caller that supplies BOTH surfaces from the same invented number is bound
    to nothing: no canonical quote is involved and no freshness is checked."""
    w = world(tmp_path, qty="100", cash="100000")
    invented = Decimal("0.01")
    dump = proposal_of("f1", [leg("f1", 0, "SGOV", Side.SELL, "100", invented)])
    out = _reserve(w, dump, {"SGOV": invented, "BIL": invented, "SHV": invented})
    reason = out.block_reason
    is_auto = out.is_auto
    w.close()
    assert is_auto is False, (
        "the invented-pair SELL reached executable AUTO with a policy valuation "
        f"of {invented} per share; block_reason={reason!r}"
    )


def test_FINDING_execution_submits_a_limit_derived_from_an_invented_pair(tmp_path):
    """The mutation boundary does not require canonical bindings: with
    price_bindings=None the submitted LIMIT is whatever the caller invented."""
    w = world(tmp_path, qty="100", cash="100000")
    invented = Decimal("0.01")
    dump = proposal_of("f2", [leg("f2", 0, "SGOV", Side.SELL, "100", invented)])
    prices = {"SGOV": invented, "BIL": invented, "SHV": invented}
    out = _reserve(w, dump, prices)
    assert out.is_auto is True
    mutate = w.mutate(fill_on_submit=False)
    execute_reserved_proposal(
        w.store, w.read(), mutate, dump, now=DEFAULT_NOW, prices=prices)
    submitted = mutate.orders.get(dump.legs[0].client_order_id)
    calls = mutate.submit_calls
    w.close()
    assert calls == 0, (
        "a 100-share SELL was submitted as a DAY LIMIT at "
        f"{submitted.get('limit_price')} - a caller-invented price, with no "
        "canonical quote and no freshness check anywhere in the path. "
        "price_bindings is an optional argument, not a precondition of mutation."
    )
