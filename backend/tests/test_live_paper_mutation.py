"""Optional live PAPER mutating smoke. Never runs by default.

Intended manual invocation later:

    pytest --live-paper-mutation tests/test_live_paper_mutation.py

This submits BUY 1 SGOV only after credentials, paper endpoint, market state,
fresh IEX quote, reconciliation, bound LIMIT, policy, and AUTO authority all pass.

A prior preflight report is not accepted as authorization. This path always
re-runs quote → recon → TreasuryGuard → authority → kill switch → LIMIT.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from opaca.broker.gateway import PAPER_ENDPOINT
from opaca.broker.paper import ENV_KEY_ID, ENV_SECRET
from opaca.domain.models import Side
from opaca.execution.gateway import assert_paper_execution_gateway
from opaca.execution.service import execute_reserved_proposal
from opaca.market.binding import bind_buy, bind_single_leg_proposal
from opaca.market.source import required_canonical_prices
from opaca.orchestration.reserve import evaluate_and_reserve
from opaca.persistence.demo import init_paper_demo_store
from opaca.persistence.types import ReconciliationStatus
from opaca.policy.client_order_id import deterministic_client_order_id
from opaca.reconciliation.service import reconcile

pytestmark = pytest.mark.live_paper_mutation


def test_live_paper_mutation_buy_one_sgov(tmp_path: Path) -> None:
    if not os.environ.get(ENV_KEY_ID, "").strip() or not os.environ.get(ENV_SECRET, "").strip():
        pytest.fail("live paper mutation requested but paper credentials are not present")
    from opaca.broker.alpaca import open_paper_gateway_from_env
    from opaca.broker.paper_execution import open_paper_execution_gateway_from_env
    from opaca.market.source import open_paper_market_data_from_env

    warning = (
        "WARNING: live PAPER mutation requested. This will submit BUY 1 SGOV "
        f"LIMIT to {PAPER_ENDPOINT}. Live money is still forbidden."
    )
    sys.stderr.write(warning + "\n")
    read = open_paper_gateway_from_env()
    mutate = open_paper_execution_gateway_from_env()
    market = open_paper_market_data_from_env()
    assert_paper_execution_gateway(mutate)
    assert mutate.endpoint == PAPER_ENDPOINT
    account = read.get_account()
    _ = account
    clock = read.get_clock()
    if clock.get("is_open") is not True:
        pytest.fail("market is not open; refusing live paper mutation")
    now = datetime.now(UTC)
    store = init_paper_demo_store(tmp_path / "opaca-paper-demo.db", now=now)
    recon = reconcile(store, read, now=now)
    if recon.status is not ReconciliationStatus.RECONCILED or recon.snapshot is None:
        pytest.fail(f"reconciliation not clean: {recon.status.value} {recon.reasons}")
    canonical = required_canonical_prices(
        market,
        proposal_symbols=("SGOV",),
        side_by_symbol={"SGOV": Side.BUY},
        positions=recon.snapshot.positions,
        now=now,
    )
    bound = bind_buy(canonical["SGOV"], Decimal("1"))
    proposal, prices, bindings = bind_single_leg_proposal("live-buy-1-sgov", bound, canonical)
    assert proposal.legs[0].client_order_id == deterministic_client_order_id("live-buy-1-sgov", 0)
    assert proposal.legs[0].reference_price == bound.limit_price
    assert prices["SGOV"] == bound.valuation_price
    reserved = evaluate_and_reserve(
        store,
        proposal,
        now=now,
        prices=prices,
        expected_snapshot_version=recon.snapshot.version,
        price_bindings=bindings,
    )
    if not reserved.is_auto:
        pytest.fail(
            f"authority is not currently executable AUTO: {reserved.authority_result} "
            f"{reserved.block_reason}"
        )
    result = execute_reserved_proposal(
        store,
        read,
        mutate,
        proposal,
        now=now,
        prices=prices,
        price_bindings=bindings,
        market_data=market,
    )
    if result.blocked:
        pytest.fail(f"execution blocked: {result.block_reason}")
    store.close()
