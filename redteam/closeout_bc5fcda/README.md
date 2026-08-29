# Closeout probes — `bc5fcda36523375c2a5690432713f0843ef451d2`

Independent probes written for the final closeout retest of
`origin/feat/treasury-core @ bc5fcda`. They were written fresh against the
five findings named for retest, not derived from the assertions already in
`redteam/`, so that the closeout has evidence that does not depend on the
existing suite being right.

They run exactly like the rest of the suite, against a worktree of the
builder commit — never against this branch:

    git worktree add --detach /tmp/tc bc5fcda36523375c2a5690432713f0843ef451d2
    OPACA_BACKEND=/tmp/tc/backend pytest -q redteam/

| file | finding retested |
| --- | --- |
| `test_price_boundary_probe.py` | P1-a `PolicyContext.prices` boundary (25) |
| `test_ledger_probe.py` | P1-b `LedgerInconsistencyError` handling (9) |
| `test_authority_window_probe.py` | P2-a future-dated authority history (16) |
| `test_calendar_input_probe.py` | P2-b calendar input range (30) |
| `test_assert_removal_probe.py` | P2-d CHECK-02 bare `assert` / `python -O` (5) |
| `test_residual_escape_probe.py` | residual "exception escapes `evaluate()`" sweep (10) |

## Status

**95 passed / 0 failed** against `bc5fcda`.

**Teeth: 50 of the 95 fail against the previous commit `5d33a05`.** Every
finding marked CLOSED in the closeout report is covered by at least one probe
that fails at `5d33a05` and passes at `bc5fcda`.

    # proves the assertions have teeth
    OPACA_BACKEND=<worktree-at-5d33a05>/backend pytest -q redteam/closeout_bc5fcda
    # -> 50 failed, 45 passed

`test_residual_escape_probe.py` also pins three P3 residuals that are **open**
at `bc5fcda` and were open at `5d33a05` too — they are characterisation tests
of unchanged behaviour, not regressions:

* a leg notional whose factors are each individually valid can overflow
  `MAGNITUDE_LIMIT`, and `MoneyError` escapes `evaluate()` instead of becoming
  a failed check (same shape inside CHECK-04's own qty x price);
* `assess_partial_fill_safety` still calls `compute_liquidity` unguarded —
  unreachable through `decide()`, which short-circuits on the failed base
  decision, but a direct caller gets the raw `LedgerInconsistencyError`;
* an invalid price raises at `PolicyContext` construction while a missing
  price becomes a failed check.

Full write-up: `claude/treasury-core-redteam-closeout-bc5fcda.md`.
