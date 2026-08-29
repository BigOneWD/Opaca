"""Phase 2 probes inherit ``redteam/conftest.py``.

That parent conftest is what puts the reviewed checkout on ``sys.path``; point
``OPACA_BACKEND`` at the ``backend/`` directory of a worktree at
``3fdabf3bcbe3e0d8c8ccfb5a9feedad584c4b6e2`` before running:

    git worktree add --detach /tmp/rc 3fdabf3bcbe3e0d8c8ccfb5a9feedad584c4b6e2
    OPACA_BACKEND=/tmp/rc/backend pytest -q redteam/reconciliation_3fdabf3

No fixtures are defined here; this file exists only to carry that note beside
the probes it applies to.
"""
