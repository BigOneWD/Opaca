"""P2-a retest: future-dated autonomous history must not vanish from the
rolling windows (fail-open on clock skew at 5d33a05)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from helpers import (  # type: ignore[import-not-found]
    DEFAULT_NOW,
    decide,
    make_context,
    make_order,
    make_proposal,
)
from opaca.authority.engine import (
    ROLLING_NOTIONAL_WINDOW,
    RUNAWAY_WINDOW,
    executions_in_window,
    rolling_count,
    rolling_notional,
)
from opaca.domain.models import AuthorityResult, AutonomousExecution, Side

SGOV = "SGOV"
SECOND = timedelta(seconds=1)


def _hist(*offsets: timedelta, notional: str = "49000") -> tuple[AutonomousExecution, ...]:
    return tuple(
        AutonomousExecution(timestamp=DEFAULT_NOW + off, notional=Decimal(notional))
        for off in offsets
    )


def _small_buy():
    # 2,000 notional: inside every per-order / per-proposal limit.
    return make_proposal(
        "p", [make_order("p", 0, SGOV, Side.BUY, Decimal("20"), Decimal("100.00"))]
    )


# --- the 5d33a05 skew exploit ----------------------------------------------


def test_past_stamped_history_requires_approval() -> None:
    ctx = make_context(cash="100000", autonomous_history=_hist(-SECOND))
    assert decide(_small_buy(), ctx).result is AuthorityResult.APPROVAL_REQUIRED


def test_future_stamped_history_no_longer_buys_auto() -> None:
    """Identical history one second in the FUTURE returned AUTO at 5d33a05."""
    ctx = make_context(cash="100000", autonomous_history=_hist(+SECOND))
    assert decide(_small_buy(), ctx).result is AuthorityResult.APPROVAL_REQUIRED


@pytest.mark.parametrize(
    "skew",
    [
        timedelta(seconds=1),
        timedelta(minutes=5),
        timedelta(hours=23),
        timedelta(hours=25),
        timedelta(days=365),
    ],
    ids=["1s", "5m", "23h", "25h", "1y"],
)
def test_any_amount_of_forward_skew_still_counts(skew: timedelta) -> None:
    ctx = make_context(cash="100000", autonomous_history=_hist(+skew))
    assert decide(_small_buy(), ctx).result is AuthorityResult.APPROVAL_REQUIRED


def test_exactly_now_counts() -> None:
    ctx = make_context(cash="100000", autonomous_history=_hist(timedelta(0)))
    assert decide(_small_buy(), ctx).result is AuthorityResult.APPROVAL_REQUIRED


# --- the 24h cutoff itself is unchanged ------------------------------------


def test_execution_exactly_one_window_old_is_still_excluded() -> None:
    hist = _hist(-ROLLING_NOTIONAL_WINDOW)
    assert executions_in_window(hist, DEFAULT_NOW, ROLLING_NOTIONAL_WINDOW) == ()
    assert rolling_notional(hist, DEFAULT_NOW) == Decimal("0")


def test_execution_one_second_inside_the_cutoff_is_included() -> None:
    hist = _hist(-ROLLING_NOTIONAL_WINDOW + SECOND)
    assert len(executions_in_window(hist, DEFAULT_NOW, ROLLING_NOTIONAL_WINDOW)) == 1


def test_stale_history_still_expires_and_allows_auto() -> None:
    """Teeth: the window must still forget, or the fix would be vacuous."""
    ctx = make_context(cash="100000", autonomous_history=_hist(-ROLLING_NOTIONAL_WINDOW))
    assert decide(_small_buy(), ctx).result is AuthorityResult.AUTO


def test_no_history_reaches_auto() -> None:
    ctx = make_context(cash="100000")
    assert decide(_small_buy(), ctx).result is AuthorityResult.AUTO


# --- count-based dimensions -------------------------------------------------


def test_future_dated_entries_count_toward_the_rolling_order_count() -> None:
    """5 old (outside the runaway hour) + 5 future entries = 10 in the 24h
    count window; the 11th order breaches rolling_order_count_max=10 while the
    runaway hourly limit (5 future + 1 = 6) is exactly at its maximum."""
    hist = _hist(*([-timedelta(hours=2)] * 5), *([+SECOND] * 5), notional="1")
    assert rolling_count(hist, DEFAULT_NOW) == 10
    assert rolling_count(hist, DEFAULT_NOW, RUNAWAY_WINDOW) == 5
    ctx = make_context(cash="100000", autonomous_history=hist)
    assert decide(_small_buy(), ctx).result is AuthorityResult.APPROVAL_REQUIRED


def test_future_dated_history_alone_can_trip_the_runaway_hard_limit() -> None:
    """Consequence of removing the upper bound: forward-skewed entries also
    count toward CHECK-13. Conservative (tightening), documented here."""
    hist = _hist(*([+SECOND] * 6), notional="1")
    assert rolling_count(hist, DEFAULT_NOW, RUNAWAY_WINDOW) == 6
    ctx = make_context(cash="100000", autonomous_history=hist)
    assert decide(_small_buy(), ctx).result is AuthorityResult.REJECT


def test_future_dated_entries_count_toward_the_runaway_hourly_limit() -> None:
    hist = _hist(*([+timedelta(minutes=30)] * 6), notional="1")
    assert rolling_count(hist, DEFAULT_NOW, RUNAWAY_WINDOW) == 6
    ctx = make_context(cash="100000", autonomous_history=hist)
    # CHECK-13 is a hard policy check: projected 7 > max 6 -> REJECT.
    assert decide(_small_buy(), ctx).result is AuthorityResult.REJECT


def test_skew_can_only_tighten_never_widen() -> None:
    """For every offset, the in-window count is monotone non-decreasing as the
    stamp moves forward."""
    offsets = [
        -timedelta(hours=48),
        -ROLLING_NOTIONAL_WINDOW,
        -timedelta(hours=1),
        timedelta(0),
        +timedelta(hours=1),
        +timedelta(hours=48),
    ]
    counts = [len(executions_in_window(_hist(o), DEFAULT_NOW, ROLLING_NOTIONAL_WINDOW)) for o in offsets]
    assert counts == sorted(counts)
