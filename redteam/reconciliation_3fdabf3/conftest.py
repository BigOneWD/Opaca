"""Phase 2 probes inherit ``redteam/conftest.py``.

That parent conftest is what puts the reviewed checkout on ``sys.path``; point
``OPACA_BACKEND`` at the ``backend/`` directory of a worktree at the commit
under review before running:

    git worktree add --detach /tmp/rc d85a2e62b5d4c3852dcd5322eb4d2c907fbec32e
    OPACA_BACKEND=/tmp/rc/backend pytest -q redteam/reconciliation_3fdabf3

No fixtures are defined here; this file exists only to carry that note beside
the probes it applies to.
"""
