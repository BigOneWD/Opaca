"""Attacks on the RT-01..RT-10 remediation itself (target d06f8ea).

Everything here targets machinery that did not exist at 2c5a6d8: the
Amendment G investment pool base, sell reservations, the wired authority
path, calendar range bounds and the magnitude limit.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from helpers import DEFAULT_NOW, ENGINE, decide, evaluate, make_context, make_order, make_proposal
from opaca.authority.engine import apply_human_approval, decide_authority
from opaca.calendar.us_trading_calendar import (
    SUPPORTED_RANGE_END, SUPPORTED_RANGE_START, CalendarError, StaticTradingCalendar,
    TradingSession, US_TRADING_CALENDAR,
)
from opaca.domain.models import (
    AuthorityPolicy, AuthorityResult, CheckId, InvestmentPolicy, Obligation,
    OrderState, Position, PrecloseBlackoutConfig, Side, UnresolvedOrder,
)
from opaca.policy.engine import effective_available_quantity, sell_reservations
from opaca.policy.partial_fill import assess_partial_fill_safety

BIGAUTH = AuthorityPolicy(Decimal("1e9"), Decimal("1e9"), Decimal("1e9"), 500, 500)
PRICES = {"SGOV": Decimal("100.00"), "BIL": Decimal("100.00"), "XYZ": Decimal("100.00")}


def _pol(conc="0.70"):
    return InvestmentPolicy(
        permitted_symbols=frozenset({"SGOV", "BIL", "SHV"}),
        concentration_max_fraction=Decimal(conc),
        min_trade_notional=Decimal("1.00"),
        preclose_blackout=PrecloseBlackoutConfig(enabled=True, minutes_before_close=15),
    )


def _ctx(**kw):
    kw.setdefault("cash", "1000000")
    kw.setdefault("obligations", ())
    kw.setdefault("operating_reserve", Decimal("0"))
    kw.setdefault("authority_policy", BIGAUTH)
    kw.setdefault("investment_policy", _pol())
    kw.setdefault("prices", PRICES)
    return make_context(**kw)


# =============================================================== NEW FINDINGS

def test_NEW01_non_permitted_holding_gives_no_denominator_and_no_headroom():
    """NEW-01 corrected behaviour: a holding in a NON-permitted symbol is not an
    eligible investment holding. It must not enter the investment pool base, it
    must not buy concentration headroom for a permitted symbol, and it must not
    become an offender in its own right. CHECK-03 keeps prohibiting trading it."""

    def max_passing_buy(xyz_value):
        positions = ()
        if xyz_value:
            positions = (Position("XYZ", Decimal(xyz_value) / Decimal("100"),
                                  Decimal("0"), Decimal(xyz_value)),)
        ctx = _ctx(cash="100000", positions=positions)
        passing = []
        for notional in (70000, 70001, 80000, 100000, 120000, 140000, 1000000):
            prop = make_proposal("n1", [make_order(
                "n1", 0, "SGOV", Side.BUY, str(Decimal(notional) / Decimal("100")), "100.00")])
            if evaluate(prop, ctx).result_for(CheckId.CHECK_04).passed:
                passing.append(notional)
        return max(passing) if passing else None

    # 70% of a 100,000 pool, with no ineligible holding in sight
    assert max_passing_buy(0) == 70000
    # the SAME budget however large the ineligible holding is: no headroom bought
    assert max_passing_buy(100000) == 70000
    assert max_passing_buy(1000000) == 70000
    assert max_passing_buy(10000000) == 70000

    # and the denominator itself is unmoved by the ineligible holding
    def pool_base(positions):
        ctx = _ctx(cash="100000", positions=positions)
        prop = make_proposal("n1c", [make_order("n1c", 0, "SGOV", Side.BUY, "1", "100.00")])
        detail = evaluate(prop, ctx).result_for(CheckId.CHECK_04).detail
        return detail.split("investment pool base ")[1].split(" ")[0]

    xyz = Position("XYZ", Decimal("100000"), Decimal("0"), Decimal("10000000"))
    assert pool_base(()) == pool_base((xyz,)) == "100000.00"

    # an ineligible holding that dwarfs the pool is NOT itself an offender and
    # must not block otherwise-compliant treasury activity
    sg = Position("SGOV", Decimal("100"), Decimal("100"), Decimal("10000"))
    ctx = _ctx(cash="1", operating_reserve=Decimal("1"), positions=(sg, xyz))
    keep = make_proposal("n1d", [make_order("n1d", 0, "SGOV", Side.SELL, "1", "100.00")])
    assert evaluate(keep, ctx).result_for(CheckId.CHECK_04).passed

    # XYZ is genuinely not investable under policy:
    ctx = _ctx(cash="100000")
    xyz_buy = make_proposal("n1b", [make_order("n1b", 0, "XYZ", Side.BUY, "1", "100.00")])
    assert not evaluate(xyz_buy, ctx).result_for(CheckId.CHECK_03).passed


def test_NEW02_idempotent_retry_of_the_same_proposal_is_blocked_by_its_own_reservation():
    """ATTACK: CHECK-09 exists so a logical leg can be safely retried under a
    deterministic client_order_id. The RT-01 reservation counts the
    proposal's OWN unresolved order, so re-evaluating it for recovery now
    fails CHECK-16."""
    pos = Position("SGOV", Decimal("100"), Decimal("100"), Decimal("10000"))
    own = UnresolvedOrder(
        proposal_id="P1", symbol="SGOV", side=Side.SELL,
        client_order_id="opaca-own", state=OrderState.UNKNOWN,
        quantity=Decimal("100"), filled_quantity=Decimal("0"),
    )
    ctx = _ctx(positions=(pos,), unresolved_orders=(own,))
    retry = make_proposal("P1", [make_order("P1", 0, "SGOV", Side.SELL, "100", "100.00")])
    r = evaluate(retry, ctx).result_for(CheckId.CHECK_16)
    assert not r.passed
    assert "reserved unresolved sells 100" in r.detail


def test_NEW03_monotonic_de_risking_is_allowed_from_a_pre_existing_breach():
    """NEW-03 corrected behaviour: from a PRE-EXISTING concentration breach the
    projection may remain above the limit provided every pre-existing offender
    STRICTLY improves and no previously compliant symbol becomes a new
    offender. 95% -> 85% against a 70% limit is a PASS (monotonic de-risking).

    The rule is improvement-based, not side-based: a sell that does not improve
    an offender, or that creates a new one, must still FAIL."""
    def state():
        sg = Position("SGOV", Decimal("950"), Decimal("950"), Decimal("95000"))
        bi = Position("BIL", Decimal("50"), Decimal("50"), Decimal("5000"))
        return _ctx(cash="1", operating_reserve=Decimal("1"), positions=(sg, bi))

    ctx = state()
    # pool base is 100,000: SGOV is pre-existing at 95%, BIL compliant at 5%
    noop = evaluate(make_proposal("n3z", []), ctx).result_for(CheckId.CHECK_04)
    assert not noop.passed, "a no-op from a breached state must not pass"
    assert "0.95" in noop.detail, noop.detail        # 95,000 / 100,000 pool base

    def check04(pid, legs):
        return evaluate(make_proposal(pid, legs), ctx).result_for(CheckId.CHECK_04)

    # 95% -> 85%: still above the 70% limit, but strictly improving => PASS
    improving = check04("n3a", [make_order("n3a", 0, "SGOV", Side.SELL, "100", "100.00")])
    assert improving.passed, improving.detail
    assert "monotonic" in improving.detail

    # 95% -> 70% (exactly at the limit) and 95% -> 65% both PASS
    assert check04("n3b", [make_order("n3b", 0, "SGOV", Side.SELL, "250", "100.00")]).passed
    assert check04("n3c", [make_order("n3c", 0, "SGOV", Side.SELL, "300", "100.00")]).passed

    # full liquidation of the offender PASSES
    assert check04("n3d", [make_order("n3d", 0, "SGOV", Side.SELL, "950", "100.00")]).passed

    # --- the rule still has teeth -------------------------------------------
    # selling something else leaves the offender untouched => FAIL
    stale = check04("n3e", [make_order("n3e", 0, "BIL", Side.SELL, "50", "100.00")])
    assert not stale.passed and "not strictly improved" in stale.detail

    # a net-zero round trip is not an improvement => FAIL
    flat = check04("n3f", [make_order("n3f", 0, "SGOV", Side.SELL, "100", "100.00"),
                           make_order("n3f", 1, "SGOV", Side.BUY, "100", "100.00")])
    assert not flat.passed and "not strictly improved" in flat.detail

    # buying MORE of the offender => FAIL
    worse = check04("n3g", [make_order("n3g", 0, "SGOV", Side.BUY, "10", "100.00")])
    assert not worse.passed and "not strictly improved" in worse.detail

    # improving the offender while creating a NEW one => FAIL
    swap = check04("n3h", [make_order("n3h", 0, "SGOV", Side.SELL, "300", "100.00"),
                           make_order("n3h", 1, "BIL", Side.BUY, "700", "100.00")])
    assert not swap.passed and "new offender" in swap.detail


# ====================================================== RT-01 reservation logic

def test_rt01_reservation_never_double_subtracts_broker_quantity_available():
    """min(broker available, reconciled qty - reserved) — verified across the
    three cases that distinguish a correct bound from a double subtraction."""
    def eff(qty, avail, order_qty, filled):
        pos = Position("SGOV", Decimal(qty), Decimal(avail), Decimal("0"))
        uo = UnresolvedOrder("prior", "SGOV", Side.SELL, "opaca-y", OrderState.NEW,
                             quantity=Decimal(order_qty), filled_quantity=Decimal(filled))
        reserved, undet = sell_reservations((uo,))
        return effective_available_quantity(pos, reserved.get("SGOV", ZERO_D), "SGOV" in undet)

    ZERO_D = Decimal("0")
    # broker has NOT decremented: local reservation binds
    assert eff("100", "100", "60", "0") == Decimal("40")
    # broker HAS already decremented: must NOT subtract 60 twice (would be -20)
    assert eff("100", "40", "60", "0") == Decimal("40")
    # partially filled prior sell: only the remaining 40 is reserved
    assert eff("100", "60", "60", "20") == Decimal("60")


def test_rt01_unresolved_sell_now_blocks_a_second_full_position_sell():
    pos = Position("SGOV", Decimal("100"), Decimal("100"), Decimal("10000"))
    prior = UnresolvedOrder("prior", "SGOV", Side.SELL, "opaca-p", OrderState.UNKNOWN,
                            quantity=Decimal("100"), filled_quantity=Decimal("0"))
    ctx = _ctx(positions=(pos,), unresolved_orders=(prior,))
    prop = make_proposal("r1", [make_order("r1", 0, "SGOV", Side.SELL, "100", "100.00")])
    assert not evaluate(prop, ctx).result_for(CheckId.CHECK_16).passed
    assert decide(prop, ctx).result is AuthorityResult.REJECT


def test_rt01_undeterminable_remaining_quantity_fails_closed():
    pos = Position("SGOV", Decimal("100"), Decimal("100"), Decimal("10000"))
    unknown = UnresolvedOrder("prior", "SGOV", Side.SELL, "opaca-u", OrderState.UNKNOWN)
    ctx = _ctx(positions=(pos,), unresolved_orders=(unknown,))
    prop = make_proposal("r2", [make_order("r2", 0, "SGOV", Side.SELL, "1", "100.00")])
    r = evaluate(prop, ctx).result_for(CheckId.CHECK_16)
    assert not r.passed and "undeterminable" in r.detail


def test_rt01_unresolved_BUY_does_not_reserve_shares():
    pos = Position("SGOV", Decimal("100"), Decimal("100"), Decimal("10000"))
    buy = UnresolvedOrder("prior", "SGOV", Side.BUY, "opaca-b", OrderState.NEW,
                          quantity=Decimal("50"), filled_quantity=Decimal("0"))
    ctx = _ctx(positions=(pos,), unresolved_orders=(buy,))
    # a pending BUY consumes no shares, but CHECK-10 still blocks the opposing sell
    reserved, undet = sell_reservations((buy,))
    assert reserved == {} and undet == frozenset()


def test_rt01_every_unresolved_state_reserves():
    """No unresolved state may be treated as harmless."""
    from opaca.domain.models import UNRESOLVED_ORDER_STATES
    for state in UNRESOLVED_ORDER_STATES:
        uo = UnresolvedOrder("p", "SGOV", Side.SELL, "opaca-s", state,
                             quantity=Decimal("10"), filled_quantity=Decimal("0"))
        reserved, _ = sell_reservations((uo,))
        assert reserved.get("SGOV") == Decimal("10"), f"{state} did not reserve"


def test_rt01_resolved_states_do_not_reserve():
    for state in (OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED,
                  OrderState.EXPIRED, OrderState.RECONCILED, OrderState.FAILED):
        uo = UnresolvedOrder("p", "SGOV", Side.SELL, "opaca-s", state,
                             quantity=Decimal("10"), filled_quantity=Decimal("0"))
        reserved, _ = sell_reservations((uo,))
        assert reserved == {}, f"{state} should not reserve"


def test_rt01_orchestration_gap_is_documented_not_silently_claimed_solved():
    """Two simultaneous evaluations against the SAME snapshot still both pass.
    The engine docstring must say so rather than implying it is solved."""
    import opaca.policy.engine as engine_mod
    doc = engine_mod.__doc__ or ""
    assert "ATOMIC" in doc and "single-writer" in doc
    pos = Position("SGOV", Decimal("100"), Decimal("100"), Decimal("10000"))
    ctx = _ctx(positions=(pos,))
    a = evaluate(make_proposal("ca", [make_order("ca", 0, "SGOV", Side.SELL, "60", "100.00")]), ctx)
    b = evaluate(make_proposal("cb", [make_order("cb", 0, "SGOV", Side.SELL, "60", "100.00")]), ctx)
    assert a.result_for(CheckId.CHECK_16).passed and b.result_for(CheckId.CHECK_16).passed


# ==================================================== RT-02 Amendment G semantics

def test_rt02_first_buy_from_an_empty_portfolio_is_now_possible():
    ctx = _ctx(cash="1000000", positions=())
    ok = make_proposal("g1", [make_order("g1", 0, "SGOV", Side.BUY, "7000", "100.00")])
    assert evaluate(ok, ctx).result_for(CheckId.CHECK_04).passed


def test_rt02_pool_base_boundary_is_exact():
    ctx = _ctx(cash="1000000", positions=())
    at = make_proposal("g2", [make_order("g2", 0, "SGOV", Side.BUY, "7000", "100.00")])
    over = make_proposal("g3", [make_order("g3", 0, "SGOV", Side.BUY, "7001", "100.00")])
    assert evaluate(at, ctx).result_for(CheckId.CHECK_04).passed
    assert not evaluate(over, ctx).result_for(CheckId.CHECK_04).passed


def test_rt02_denominator_is_fixed_pre_trade_and_does_not_move_with_the_proposal():
    ctx = _ctx(cash="1000000", positions=())
    bases = set()
    for qty in ("1000", "3000", "5000"):
        d = evaluate(make_proposal("g4", [make_order("g4", 0, "SGOV", Side.BUY, qty, "100.00")]),
                     ctx).result_for(CheckId.CHECK_04)
        bases.add(d.detail.split("investment pool base ")[1].split(" ")[0])
    assert len(bases) == 1, f"pool base moved with the proposal: {bases}"


def test_rt02_partial_de_risk_and_full_liquidation_both_pass():
    sg = Position("SGOV", Decimal("1000"), Decimal("1000"), Decimal("100000"))
    ctx = _ctx(cash="1", positions=(sg,))
    for qty in ("300", "1000"):
        prop = make_proposal("g5", [make_order("g5", 0, "SGOV", Side.SELL, qty, "100.00")])
        assert evaluate(prop, ctx).result_for(CheckId.CHECK_04).passed


def test_rt02_missing_price_on_a_HELD_symbol_fails_closed():
    ctx = _ctx(positions=(Position("XYZ", Decimal("10"), Decimal("10"), Decimal("0")),),
               prices={"SGOV": Decimal("100.00")})
    prop = make_proposal("g6", [make_order("g6", 0, "SGOV", Side.BUY, "1", "100.00")])
    r = evaluate(prop, ctx).result_for(CheckId.CHECK_04)
    assert not r.passed and "fail closed" in r.detail


def test_rt02_negative_investable_cash_is_clamped_not_negated():
    """A liquidity shortfall must not shrink the pool below holdings value."""
    sg = Position("SGOV", Decimal("1000"), Decimal("1000"), Decimal("100000"))
    ob = Obligation("ob", "payroll", Decimal("500000"), date(2026, 9, 20))
    ctx = _ctx(cash="100000", positions=(sg,), obligations=(ob,))
    r = evaluate(make_proposal("g7", [make_order("g7", 0, "SGOV", Side.SELL, "100", "100.00")]),
                 ctx).result_for(CheckId.CHECK_04)
    assert "investment pool base 100000.00" in r.detail


# ====================================================== RT-06 authority wiring

def test_rt06_decide_authority_without_an_assessment_fails_closed():
    ctx = _ctx()
    prop = make_proposal("a1", [make_order("a1", 0, "SGOV", Side.BUY, "1", "100.00")])
    decision = ENGINE.evaluate(prop, ctx)
    assert decision.passed
    bare = decide_authority(prop, decision, ctx.authority_policy,
                            ctx.autonomous_history, ctx.execution.now)
    assert bare.result is AuthorityResult.REJECT
    assert "not assessed" in bare.reasons[0]


def test_rt06_composed_path_reaches_auto_for_a_safe_proposal():
    ctx = _ctx()
    prop = make_proposal("a2", [make_order("a2", 0, "SGOV", Side.BUY, "1", "100.00")])
    assert decide(prop, ctx).result is AuthorityResult.AUTO


def test_rt06_unsafe_partial_fill_is_REJECT_and_human_approval_cannot_override():
    """A sell proposal whose zero-fill outcome leaves an obligation uncovered."""
    sg = Position("SGOV", Decimal("1000"), Decimal("1000"), Decimal("100000"))
    ob = Obligation("ob", "payroll", Decimal("50000"), date(2026, 9, 10))
    ctx = _ctx(cash="1000", positions=(sg,), obligations=(ob,))
    prop = make_proposal("a3", [make_order("a3", 0, "SGOV", Side.SELL, "600", "100.00")])
    assessment = assess_partial_fill_safety(prop, ctx, ENGINE)
    assert not assessment.safe
    auth = decide(prop, ctx)
    assert auth.result is AuthorityResult.REJECT
    assert apply_human_approval(auth).result is AuthorityResult.REJECT


def test_rt09_sell_subsets_are_now_concentration_checked():
    """The RT-09 case: SGOV 60% / BIL 40%, full proposal sells both. The
    BIL-only subset must now be evaluated for CHECK-04."""
    from opaca.policy.partial_fill import PARTIAL_FILL_CHECKS
    assert CheckId.CHECK_04 in PARTIAL_FILL_CHECKS
    assert CheckId.CHECK_16 in PARTIAL_FILL_CHECKS
    sg = Position("SGOV", Decimal("600"), Decimal("600"), Decimal("60000"))
    bi = Position("BIL", Decimal("400"), Decimal("400"), Decimal("40000"))
    ctx = _ctx(cash="1", positions=(sg, bi), investment_policy=_pol("0.70"))
    prop = make_proposal("a4", [make_order("a4", 0, "SGOV", Side.SELL, "300", "100.00"),
                                make_order("a4", 1, "BIL", Side.SELL, "250", "100.00")])
    a = assess_partial_fill_safety(prop, ctx, ENGINE)
    # every subset of BOTH sides is enumerated now
    assert a.subsets_evaluated == 3


def test_rt06_enumeration_cost_stays_bounded():
    ctx = _ctx(cash="10000000")
    legs = [make_order("a5", i, ["SGOV", "BIL", "SHV"][i % 3], Side.BUY, "1", "100.00")
            for i in range(12)]
    start = time.time()
    a = assess_partial_fill_safety(make_proposal("a5", legs), ctx, ENGINE)
    elapsed = time.time() - start
    assert a.subsets_evaluated == 4095
    assert elapsed < 5.0, f"12-leg enumeration took {elapsed:.2f}s"


def test_rt06_leg_count_ceiling_still_fails_closed():
    ctx = _ctx(cash="10000000")
    legs = [make_order("a6", i, "SGOV", Side.BUY, "1", "100.00") for i in range(13)]
    a = assess_partial_fill_safety(make_proposal("a6", legs), ctx, ENGINE)
    assert not a.safe and a.subsets_evaluated == 0


# ============================================ RT-03/04 calendar, RT-05 money, RT-10

def test_rt03_out_of_range_dates_now_fail_closed():
    for day in (date(2028, 7, 4), date(2028, 12, 25), date(2024, 6, 3),
                SUPPORTED_RANGE_END + timedelta(days=1),
                SUPPORTED_RANGE_START - timedelta(days=1)):
        with pytest.raises(CalendarError):
            US_TRADING_CALENDAR.is_trading_day(day)
        with pytest.raises(CalendarError):
            US_TRADING_CALENDAR.session(day)


def test_rt03_supported_range_boundaries_are_inclusive():
    US_TRADING_CALENDAR.is_trading_day(SUPPORTED_RANGE_START)
    US_TRADING_CALENDAR.is_trading_day(SUPPORTED_RANGE_END)


def test_rt04_static_calendar_fails_closed_immediately_past_its_last_session():
    cal = StaticTradingCalendar([
        TradingSession(date(2026, 10, 9), datetime(2026, 1, 1, 9, 30).time(),
                       datetime(2026, 1, 1, 16, 0).time()),
        TradingSession(date(2026, 10, 12), datetime(2026, 1, 1, 9, 30).time(),
                       datetime(2026, 1, 1, 16, 0).time()),
    ])
    start = time.time()
    with pytest.raises(CalendarError):
        cal.settlement_date(date(2026, 10, 12))
    assert time.time() - start < 1.0, "must fail fast, not scan toward date.max"


def test_rt04_empty_static_calendar_fails_closed():
    with pytest.raises(CalendarError):
        StaticTradingCalendar([]).next_trading_day(date(2026, 9, 1))


def test_rt04_calendar_exhaustion_fails_checks_closed_instead_of_crashing():
    """The CalendarError must be caught by the engine and turned into a
    failed CHECK-02/CHECK-12, not propagate out of evaluate()."""
    cal = StaticTradingCalendar([TradingSession(
        date(2026, 9, 1), datetime(2026, 1, 1, 9, 30).time(),
        datetime(2026, 1, 1, 16, 0).time())])
    pos = Position("SGOV", Decimal("100"), Decimal("100"), Decimal("10000"))
    ctx = _ctx(positions=(pos,), calendar=cal)
    prop = make_proposal("c1", [make_order("c1", 0, "SGOV", Side.SELL, "10", "100.00")])
    d = evaluate(prop, ctx)          # must NOT raise
    assert not d.result_for(CheckId.CHECK_02).passed
    assert not d.result_for(CheckId.CHECK_12).passed
    assert not d.passed


def test_rt05_magnitude_limit_raises_MoneyError_not_InvalidOperation():
    from decimal import InvalidOperation
    from opaca.domain.money import MAGNITUDE_LIMIT, MoneyError, money, round_budget
    from opaca.domain.models import BrokerCashState, ProposedOrder
    from opaca.treasury.scenario import seed_scenario

    money(MAGNITUDE_LIMIT - Decimal("1"))
    for bad in (MAGNITUDE_LIMIT, Decimal("9") * MAGNITUDE_LIMIT, -MAGNITUDE_LIMIT):
        with pytest.raises(MoneyError):
            money(bad)
    with pytest.raises(MoneyError):
        round_budget(Decimal("9") * MAGNITUDE_LIMIT)
    with pytest.raises(MoneyError):
        BrokerCashState(Decimal("1e30"), Decimal("0"), Decimal("0"), Decimal("1"), DEFAULT_NOW)
    with pytest.raises(MoneyError):
        ProposedOrder("big", 0, "SGOV", Side.BUY, Decimal("1e19"), Decimal("100.69"), "opaca-x")
    with pytest.raises(MoneyError):
        seed_scenario(Decimal("1e30"), date(2026, 9, 1))
    # and every one of them is a ValueError, never a bare ArithmeticError
    try:
        money(Decimal("1e30"))
    except Exception as exc:
        assert isinstance(exc, ValueError) and not isinstance(exc, InvalidOperation)


def test_rt10_partial_evaluation_can_never_report_a_complete_pass():
    ctx = _ctx()
    prop = make_proposal("d1", [make_order("d1", 0, "SGOV", Side.BUY, "1", "100.00")])
    full = evaluate(prop, ctx)
    assert full.passed and full.complete
    narrow = ENGINE.evaluate(prop, ctx, only=frozenset({CheckId.CHECK_03}))
    assert not narrow.passed
    assert not narrow.complete
    assert narrow.result_for(CheckId.CHECK_03).passed   # detail still readable


def test_rt07_trading_day_gate_is_unconditional():
    from opaca.domain.models import PrecloseBlackoutConfig as PBC
    off = InvestmentPolicy(frozenset({"SGOV", "BIL", "SHV"}), Decimal("0.70"),
                           Decimal("1.00"), PBC(enabled=False, minutes_before_close=15))
    prop = make_proposal("e1", [make_order("e1", 0, "SGOV", Side.BUY, "1", "100.00")])
    for moment, label in (
        (datetime(2026, 9, 7, 14, 30, tzinfo=timezone.utc), "Labor Day"),
        (datetime(2026, 9, 5, 14, 30, tzinfo=timezone.utc), "Saturday"),
    ):
        ctx = make_context(now=moment, seed_date=moment.date(), investment_policy=off,
                           authority_policy=BIGAUTH, obligations=(),
                           operating_reserve=Decimal("0"), prices=PRICES, cash="1000000")
        d = evaluate(prop, ctx)
        assert not d.result_for(CheckId.CHECK_15).passed, f"{label} passed with blackout off"
        assert decide(prop, ctx).result is AuthorityResult.REJECT
