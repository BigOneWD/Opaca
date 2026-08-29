# Treasury Core — red-team suite

Adversarial tests written to **falsify** the builder's report for

    origin/feat/treasury-core @ 2c5a6d81a87ccc66547ac7519411b3fe516ef664

These tests are red-team-only and live on `review/treasury-red-team`.
They are never added to the builder branch and they do not import from this
branch — they run against a checkout of the builder commit:

    git worktree add --detach /tmp/tc 2c5a6d81a87ccc66547ac7519411b3fe516ef664
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

Tests named `*_NOT_*`, `*_do_NOT_*` or carrying an `ATTACK:` docstring assert
**current** behaviour that the red team considers defective. They are
characterisation tests: they will fail once the corresponding finding is fixed,
which is the intended signal.
