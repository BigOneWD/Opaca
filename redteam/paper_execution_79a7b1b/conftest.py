"""Phase 3 probes inherit ``redteam/conftest.py``.

Point ``OPACA_BACKEND`` at the ``backend/`` directory of a worktree at the
commit under review:

    git worktree add --detach /tmp/pe 79a7b1b837c86dc700533eeda6f5699b197a7d4a
    OPACA_BACKEND=/tmp/pe/backend pytest -q redteam/paper_execution_79a7b1b

No fixtures are defined here; this file exists only to carry that note beside
the probes it applies to.
"""
