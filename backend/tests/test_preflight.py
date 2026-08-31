"""Read-only preflight: zero broker mutation; stale report cannot authorize."""

from __future__ import annotations

import ast
from pathlib import Path

from opaca.broker.mutation import FORBIDDEN_BROKER_MUTATIONS
from opaca.execution.service import execute_reserved_proposal
from opaca.market.source import FakeMarketData
from opaca.persistence.demo import PAPER_DEMO_DB_NAME, init_paper_demo_store
from opaca.preflight import (
    EXECUTION_NOT_ATTEMPTED,
    PREFLIGHT_PROPOSAL_ID,
    PreflightReport,
    credentials_present,
    not_run_report,
    run_read_only_preflight,
)
from opaca.reconciliation.service import reconcile

from tests.execution_helpers import make_world
from tests.helpers import DEFAULT_NOW
from tests.market_helpers import universe_quotes
from tests.state_helpers import paper_gateway

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT_SOURCE = BACKEND_ROOT / "opaca" / "preflight.py"


class TestOfflinePreflight:
    def test_preflight_report_and_zero_mutation(self, tmp_path: Path) -> None:
        db_path = tmp_path / PAPER_DEMO_DB_NAME
        market = FakeMarketData(quotes=universe_quotes())
        report = run_read_only_preflight(
            paper_gateway(),
            market,
            now=DEFAULT_NOW,
            db_path=db_path,
        )
        assert report.ran is True
        assert report.paper_account == "ACTIVE"
        assert report.cash is not None
        assert report.quote_symbol == "SGOV"
        assert report.quote_price is not None
        assert report.limit_price is not None
        assert report.max_buy_notional == report.limit_price
        assert report.treasuryguard == "PASS"
        assert report.authority == "AUTO"
        assert "schema v2" in report.db_schema
        assert report.db_fresh is True
        assert report.execution == EXECUTION_NOT_ATTEMPTED
        rendered = report.render()
        assert "EXECUTION:" in rendered
        assert "NOT ATTEMPTED" in rendered
        assert not hasattr(report, "authorize")
        assert PREFLIGHT_PROPOSAL_ID not in _proposal_ids(db_path)

    def test_preflight_performs_zero_mutation_calls(self) -> None:
        source = PREFLIGHT_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        hits: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_BROKER_MUTATIONS:
                hits.append(node.attr)
            if isinstance(node, ast.FunctionDef) and node.name in FORBIDDEN_BROKER_MUTATIONS:
                hits.append(node.name)
        assert hits == []
        assert "execute_reserved_proposal" not in source
        assert "open_paper_execution_gateway_from_env" not in source

    def test_existing_demo_db_not_overwritten(self, tmp_path: Path) -> None:
        db_path = tmp_path / PAPER_DEMO_DB_NAME
        existing = init_paper_demo_store(db_path, now=DEFAULT_NOW)
        existing.close()
        report = run_read_only_preflight(
            paper_gateway(),
            FakeMarketData(quotes=universe_quotes()),
            now=DEFAULT_NOW,
            db_path=db_path,
        )
        assert report.execution == EXECUTION_NOT_ATTEMPTED
        assert report.fail_reason is not None
        assert "overwrite" in report.fail_reason

    def test_stale_preflight_cannot_later_authorize_execution(self, tmp_path: Path) -> None:
        db_path = tmp_path / PAPER_DEMO_DB_NAME
        report = run_read_only_preflight(
            paper_gateway(),
            FakeMarketData(quotes=universe_quotes()),
            now=DEFAULT_NOW,
            db_path=db_path,
        )
        assert report.authority == "AUTO"
        assert report.execution == EXECUTION_NOT_ATTEMPTED
        world = make_world(tmp_path)
        from tests.execution_helpers import buy_one
        from tests.helpers import DEFAULT_PRICES

        proposal = buy_one("after-preflight")
        recon = reconcile(world.store, world.read(), now=DEFAULT_NOW)
        assert recon.snapshot is not None
        mutate = world.mutate()
        result = execute_reserved_proposal(
            world.store,
            world.read(),
            mutate,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
        )
        assert result.submitted is False
        assert result.blocked is True
        assert mutate.submit_calls == 0
        world.store.close()

    def test_credentials_absent_is_not_run(self) -> None:
        report = not_run_report("credentials absent", db_path=PAPER_DEMO_DB_NAME)
        assert report.ran is False
        assert report.execution == EXECUTION_NOT_ATTEMPTED
        _ = credentials_present()


def _proposal_ids(path: Path) -> set[str]:
    from opaca.persistence.demo import open_existing_paper_demo_store

    store = open_existing_paper_demo_store(path)
    try:
        rows = store._conn.execute("SELECT proposal_id FROM proposals").fetchall()
        return {str(row["proposal_id"]) for row in rows}
    finally:
        store.close()


def test_preflight_report_type_is_observational() -> None:
    assert "authorize" not in PreflightReport.__annotations__
    assert "submit" not in PreflightReport.__annotations__
