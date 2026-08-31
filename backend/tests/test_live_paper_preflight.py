"""Optional live PAPER read-only preflight. Never mutates broker state."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from opaca.broker.paper import ENV_KEY_ID, ENV_SECRET
from opaca.persistence.demo import PAPER_DEMO_DB_NAME
from opaca.preflight import EXECUTION_NOT_ATTEMPTED, run_live_preflight_from_env


@pytest.mark.live_paper_preflight
def test_live_paper_read_only_preflight(tmp_path: Path) -> None:
    if not os.environ.get(ENV_KEY_ID, "").strip() or not os.environ.get(ENV_SECRET, "").strip():
        pytest.fail("live paper preflight requested but paper credentials are not present")
    report = run_live_preflight_from_env(
        db_path=tmp_path / PAPER_DEMO_DB_NAME,
        overwrite_db=False,
    )
    assert report.execution == EXECUTION_NOT_ATTEMPTED
    assert report.ran is True
    assert report.paper_account in {"ACTIVE", "FAIL"}
