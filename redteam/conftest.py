"""Red-team suite: runs against a CHECKOUT OF THE BUILDER COMMIT, never against
this branch. Point OPACA_BACKEND at the backend/ directory of a worktree at
2c5a6d81a87ccc66547ac7519411b3fe516ef664.

    git worktree add --detach /tmp/tc 2c5a6d81a87ccc66547ac7519411b3fe516ef664
    OPACA_BACKEND=/tmp/tc/backend pytest redteam/
"""
import os
import sys
from pathlib import Path

BACKEND = Path(os.environ.get("OPACA_BACKEND", "")).expanduser()
if not BACKEND.is_dir():
    raise RuntimeError(
        "set OPACA_BACKEND to the backend/ dir of a worktree at the builder commit"
    )
for p in (str(BACKEND), str(BACKEND / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)
