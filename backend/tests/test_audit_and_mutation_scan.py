"""Audit trail correspondence and broker-mutation architectural scan."""

from __future__ import annotations

import ast
from pathlib import Path

from opaca.broker.gateway import FakeAlpacaGateway, gateway_methods_are_read_only
from opaca.broker.mutation import FORBIDDEN_BROKER_MUTATIONS
from opaca.domain.models import AuthorityResult, Side
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.types import AuditEventType, ReconciliationStatus
from opaca.reconciliation.service import reconcile

from tests.helpers import DEFAULT_NOW, DEFAULT_PRICES, make_order, make_proposal
from tests.state_helpers import paper_gateway, position_payload, temp_store

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOT = BACKEND_ROOT / "opaca"
GUARD_FILES = frozenset(
    {
        str(PRODUCTION_ROOT / "broker" / "mutation.py"),
        str(PRODUCTION_ROOT / "broker" / "gateway.py"),
        str(PRODUCTION_ROOT / "broker" / "alpaca.py"),
        str(PRODUCTION_ROOT / "broker" / "paper_execution.py"),
        str(PRODUCTION_ROOT / "execution" / "gateway.py"),
        str(PRODUCTION_ROOT / "execution" / "service.py"),
    }
)
SUBMIT_IMPL_FILES = frozenset(
    {
        str(PRODUCTION_ROOT / "broker" / "paper_execution.py"),
        str(PRODUCTION_ROOT / "execution" / "gateway.py"),
    }
)


def _attribute_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


class TestAudit:
    def test_n_audit_events_correspond_to_decisions(self, tmp_path: Path) -> None:
        store = temp_store(tmp_path)
        recon = reconcile(
            store,
            paper_gateway(positions=(position_payload(qty="100"),)),
            now=DEFAULT_NOW,
        )
        assert recon.status is ReconciliationStatus.RECONCILED
        assert store.list_audit(event_type=AuditEventType.BROKER_STATE_READ)
        assert store.list_audit(event_type=AuditEventType.RECONCILIATION_COMPLETE)
        assert store.list_audit(event_type=AuditEventType.SCENARIO_SEEDED)
        proposal = make_proposal(
            "audited",
            [make_order("audited", 0, "SGOV", Side.SELL, "5", DEFAULT_PRICES["SGOV"])],
        )
        outcome = evaluate_and_reserve(
            store,
            proposal,
            now=DEFAULT_NOW,
            prices=DEFAULT_PRICES,
            expected_snapshot_version=recon.snapshot.version if recon.snapshot else None,
        )
        assert outcome.authority_result is AuthorityResult.AUTO
        evaluated = store.list_audit(
            event_type=AuditEventType.PROPOSAL_EVALUATED, proposal_id="audited"
        )
        reserved = store.list_audit(
            event_type=AuditEventType.RESERVATION_CREATED, proposal_id="audited"
        )
        assert evaluated
        assert reserved
        reject = make_proposal(
            "blocked-buy",
            [make_order("blocked-buy", 0, "SGOV", Side.BUY, "3972", "100.70")],
        )
        prices = {
            "SGOV": DEFAULT_PRICES["SGOV"],
            "BIL": DEFAULT_PRICES["BIL"],
            "SHV": DEFAULT_PRICES["SHV"],
        }
        denied = evaluate_and_reserve(
            store,
            reject,
            now=DEFAULT_NOW,
            prices=prices,
            expected_snapshot_version=recon.snapshot.version if recon.snapshot else None,
        )
        assert denied.authority_result is AuthorityResult.REJECT
        assert store.list_audit(
            event_type=AuditEventType.POLICY_REJECTED, proposal_id="blocked-buy"
        )
        store.close()


class TestMutationScan:
    def test_o_no_mutation_methods_on_phase_path(self) -> None:
        assert gateway_methods_are_read_only(FakeAlpacaGateway)
        offenders: list[str] = []
        for path in PRODUCTION_ROOT.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            names = _attribute_names(tree)
            hits = names & FORBIDDEN_BROKER_MUTATIONS
            if not hits:
                continue
            if str(path) in GUARD_FILES:
                continue
            offenders.append(f"{path.relative_to(BACKEND_ROOT)}: {sorted(hits)}")
        assert offenders == []

    def test_forbidden_set_covers_alpaca_py_mutators(self) -> None:
        required = {
            "submit_order",
            "cancel_order_by_id",
            "cancel_orders",
            "replace_order_by_id",
            "close_position",
            "close_all_positions",
            "exercise_options_position",
        }
        assert required <= FORBIDDEN_BROKER_MUTATIONS

    def test_live_gateway_source_has_no_mutation_calls(self) -> None:
        source = (PRODUCTION_ROOT / "broker" / "alpaca.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
        assert not (called & FORBIDDEN_BROKER_MUTATIONS)

    def test_submit_order_impl_only_on_execution_gateway(self) -> None:
        impls: list[str] = []
        for path in PRODUCTION_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "submit_order":
                    impls.append(str(path))
        assert set(impls) <= SUBMIT_IMPL_FILES
        assert str(PRODUCTION_ROOT / "broker" / "paper_execution.py") in impls

    def test_execution_mutation_scan_no_live_endpoint_or_generic_client(self) -> None:
        live = "https://api.alpaca.markets"
        for rel in ("execution/gateway.py", "broker/paper_execution.py", "execution/service.py"):
            source = (PRODUCTION_ROOT / rel).read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == live:
                    if rel == "execution/service.py":
                        continue
                    raise AssertionError(f"{rel} embeds live endpoint")
            assert "getattr(" not in source or rel == "execution/gateway.py"
        service = (PRODUCTION_ROOT / "execution" / "service.py").read_text(encoding="utf-8")
        assert "LIVE_ENDPOINT" in service
        gateway_src = (PRODUCTION_ROOT / "execution" / "gateway.py").read_text(encoding="utf-8")
        assert "LIVE_ENDPOINT" in gateway_src

    def test_no_bare_assert_in_production(self) -> None:
        offenders: list[str] = []
        for path in PRODUCTION_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assert):
                    offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{node.lineno}")
        assert offenders == []

    def test_no_credentials_in_fixtures_or_source(self) -> None:
        forbidden_literals = ('secret_key="', "secret_key='", "AKIA")
        for path in PRODUCTION_ROOT.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in forbidden_literals:
                assert needle not in text
        for path in (BACKEND_ROOT / "tests").rglob("*.py"):
            if path.name == "test_audit_and_mutation_scan.py":
                continue
            text = path.read_text(encoding="utf-8")
            assert 'secret_key="' not in text
            assert "AKIA" not in text
