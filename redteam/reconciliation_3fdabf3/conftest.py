"""Phase 2 probes inherit ``redteam/conftest.py``.

That parent conftest is what puts the reviewed checkout on ``sys.path``; point
``OPACA_BACKEND`` at the ``backend/`` directory of a worktree at the commit
under review before running:

    git worktree add --detach /tmp/rc 624439fbba9a2f70110e4c413a7783eda564418a
    OPACA_BACKEND=/tmp/rc/backend pytest -q redteam/reconciliation_3fdabf3

No fixtures are defined here; this file exists only to carry that note beside
the probes it applies to.
"""
