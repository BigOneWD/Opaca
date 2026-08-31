"""Final closeout probes inherit ``redteam/conftest.py``.

Point ``OPACA_BACKEND`` at the ``backend/`` directory of a worktree at the
commit under review:

    git worktree add --detach /tmp/cl 193d7a21cc956d2688f69e339cb79fc44cd34380
    OPACA_BACKEND=/tmp/cl/backend pytest -q redteam/closeout_193d7a2

No fixtures are defined here; this file exists only to carry that note beside
the probes it applies to. ``closeout_support.py`` is imported by pytest's
prepend import mode, exactly as ``p3_support.py`` is in
``paper_execution_79a7b1b/``. It is deliberately NOT named ``support.py``: that
basename is already taken by ``prelive_11d1cde/`` and prepend import mode would
bind whichever suite was collected first.

These probes run UNSHIMMED. Do not apply ``contract_adapter.py`` to them - it
manufactures canonical bindings from caller-supplied prices and pins the
boundary clock, which are the two things P1-1 and P1-2 are about.
"""
