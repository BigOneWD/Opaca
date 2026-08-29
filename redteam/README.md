# Treasury Core — red-team suite

Adversarial tests written to **falsify** the builder's report for

    origin/feat/treasury-core @ d06f8ea7e5f46c50928e331aca916e37b0aa15a1

These tests are red-team-only and live on `review/treasury-red-team`.
They are never added to the builder branch and they do not import from this
branch — they run against a checkout of the builder commit:

    git worktree add --detach /tmp/tc d06f8ea7e5f46c50928e331aca916e37b0aa15a1
    OPACA_BACKEND=/tmp/tc/backend pytest -q redteam/

| file | attack class |
| --- | --- |
| `test_p0_a_leverage.py` | P0-A cash / leverage isolation |
| `test_p0_b_settlement.py` | P0-B settlement false liquidity, double counting |
| `test_p0_c_longonly.py` | P0-C long-only / oversell, cross-proposal |
| `test_p0_d_authority.py` | P0-D authority splitting, rolling-window boundaries |
| `test_p1_b_partialfill.py` | P1-B partial-fill subset enumeration |
| `test_p1_c_calendar.py` | P1-C calendar / settlement / blackout |
| `test_p1_de_failclosed_money.py` | P1-D fail-closed, P1-E money / Decimal |
| `test_p2_interpretations.py` | P2 spec-interpretation consequences |
| `test_rt_fixes.py` | attacks on the RT-01..RT-10 remediation itself |

## Status against d06f8ea

**119 passed / 0 failed.** The suite is GREEN against correct behaviour.

The 15 tests that characterised RT-01..RT-10 at 2c5a6d8 have been rewritten to
assert the corrected behaviour. They are genuine regression tests, not vacuous
ones: run against the pre-fix commit they still fail 15/15, and
`test_rt_fixes.py` cannot even import there.

    # proves the assertions have teeth
    OPACA_BACKEND=<worktree-at-2c5a6d8>/backend pytest -q redteam/ \
        --ignore=redteam/test_rt_fixes.py     # -> 15 failed, 74 passed

### Open characterisation tests

Three tests in `test_rt_fixes.py` pin findings that are still OPEN. They pass
today because they assert current (defective) behaviour, and they must be
inverted when the finding is fixed:

* `test_NEW01_non_whitelisted_holding_inflates_the_concentration_denominator`
* `test_NEW02_idempotent_retry_of_the_same_proposal_is_blocked_by_its_own_reservation`
* `test_NEW03_improving_but_not_curing_sell_is_rejected_from_an_overconcentrated_state`

Everything else asserts behaviour the red team believes is correct.
