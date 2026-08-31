"""Pre-live readiness probes inherit ``redteam/conftest.py``.

Point ``OPACA_BACKEND`` at the ``backend/`` directory of a worktree at the
commit under review:

    git worktree add --detach /tmp/pl 11d1cdeb4d283ba68264823e500ec14c58bf7324
    OPACA_BACKEND=/tmp/pl/backend pytest -q redteam/prelive_11d1cde

No fixtures are defined here; this file exists only to carry that note beside
the probes it applies to. ``support.py`` is imported by pytest's prepend
import mode, exactly as ``p3_support.py`` is in ``paper_execution_79a7b1b/``.
"""
