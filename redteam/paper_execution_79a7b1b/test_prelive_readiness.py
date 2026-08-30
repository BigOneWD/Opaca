"""Pre-live readiness, reported separately and NOT fixed here.

Two questions the brief asks before a real PAPER mutation:
  * does a real mutation rely on synthetic/test prices for safety?
  * what does schema v2 require of an existing database?
"""

from __future__ import annotations

import ast
import pathlib
from decimal import Decimal

import pytest
from opaca.persistence.schema import SCHEMA_VERSION
from opaca.persistence.store import PersistenceError, SQLiteStore

from p3_support import DEFAULT_NOW, DEFAULT_PRICES, SGOV, buy, reserve, sell, world

import opaca

PKG = pathlib.Path(opaca.__file__).resolve().parent
REPO = PKG.parents[1]


# ---------------------------------------------------------------- LIVE PRICES


def test_the_package_contains_no_market_data_client():
    """Establishes the fact the readiness call rests on."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in PKG.rglob("*.py"))
    for marker in (
        "get_latest_trade", "get_latest_quote", "StockHistoricalDataClient",
        "StockLatestTradeRequest", "get_stock_latest", "market_data",
    ):
        assert marker not in text, marker


def test_prices_are_a_caller_supplied_input_to_both_safety_paths():
    reserve_src = (PKG / "orchestration" / "reserve.py").read_text(encoding="utf-8")
    execute_src = (PKG / "execution" / "service.py").read_text(encoding="utf-8")
    assert "prices: Mapping[str, Decimal]" in reserve_src
    assert "prices: Mapping[str, Decimal]" in execute_src


def test_FINDING_the_live_mutation_smoke_prices_the_order_from_test_constants():
    """The only path that would place a real order sources its prices from
    tests/helpers.py DEFAULT_PRICES - fixed constants, one of them a fill price
    observed on 2026-08-28 and two with no evidence basis at all."""
    live = REPO / "backend" / "tests" / "test_live_paper_mutation.py"
    if not live.exists():
        pytest.skip("live mutation smoke absent")
    source = live.read_text(encoding="utf-8")
    assert "DEFAULT_PRICES" in source, "probe assumption"
    tree = ast.parse(source)
    uses = sorted({
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "DEFAULT_PRICES"
    })
    helpers = (REPO / "backend" / "tests" / "helpers.py").read_text(encoding="utf-8")
    assert 'PHASE1_SGOV_PRICE = Decimal("100.69")' in helpers
    assert "fixed deterministic constants are used instead" in helpers
    pytest.fail(
        "PRE-LIVE BLOCKER: test_live_paper_mutation.py passes tests.helpers."
        f"{uses[0]} to BOTH evaluate_and_reserve() and execute_reserved_proposal(), "
        "and uses it as the leg reference_price. Those are hard-coded constants "
        "(SGOV 100.69 from a 2026-08-28 fill; BIL and SHV have no evidence basis), "
        "and no market-data client exists anywhere in the package. A real paper "
        "order would therefore be sized and authorised against a stale synthetic "
        "price."
    )


def test_FINDING_a_wrong_price_changes_the_authority_outcome(tmp_path):
    """Not cosmetic: reference_price sets the notional, and the notional is what the
    delegated-authority limits and the concentration check are measured against."""
    from opaca.domain.models import Side

    from p3_support import make_order, make_proposal

    honest_dir = tmp_path / "honest"
    honest_dir.mkdir()
    wrong_dir = tmp_path / "wrong"
    wrong_dir.mkdir()

    w1 = world(honest_dir, qty="0", cash="500000")
    honest = make_proposal("b1", [make_order("b1", 0, "SGOV", Side.BUY, "300", SGOV)])
    _, out_honest = reserve(w1, honest)
    honest_notional = honest.total_buy_notional
    honest_result = out_honest.authority_result
    honest_auto = out_honest.is_auto
    w1.close()

    w2 = world(wrong_dir, qty="0", cash="500000")
    understated = make_proposal(
        "b1", [make_order("b1", 0, "SGOV", Side.BUY, "300", Decimal("10.00"))]
    )
    _, out_wrong = reserve(w2, understated)
    wrong_notional = understated.total_buy_notional
    wrong_result = out_wrong.authority_result
    wrong_auto = out_wrong.is_auto
    w2.close()

    assert honest_auto is False, "probe assumption: the true price is outside authority"
    assert wrong_auto is True, "probe assumption: the understated price is inside it"
    pytest.fail(
        "PRE-LIVE BLOCKER (impact): the SAME 300-share BUY is "
        f"{honest_notional} of exposure at the real price ({honest_result.value}, "
        f"is_auto={honest_auto}) and {wrong_notional} at a wrong one "
        f"({wrong_result.value}, is_auto={wrong_auto}). A stale reference_price does "
        "not merely mis-report size - it flips the delegated-authority decision, and "
        "it is also the settlement-proceeds fallback when the broker omits "
        "filled_avg_price."
    )


# ---------------------------------------------------------------- SCHEMA V2


def test_schema_version_is_two():
    assert SCHEMA_VERSION == 2


def test_a_fresh_database_bootstraps_cleanly(tmp_path):
    store = SQLiteStore(tmp_path / "fresh.sqlite")
    try:
        assert store.schema_version() == SCHEMA_VERSION
        assert store.journal_mode() == "WAL"
        assert store.foreign_keys_enabled()
        tables = {
            row["name"] for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"execution_orders", "approval_grants"} <= tables
    finally:
        store.close()


def test_FINDING_a_version_one_database_fails_closed_with_no_migration(tmp_path):
    """There is no migration path: bootstrap compares MAX(version) to SCHEMA_VERSION
    and raises. An existing v1 database cannot be opened or upgraded."""
    path = tmp_path / "v1.sqlite"
    store = SQLiteStore(path)
    store._conn.execute("DELETE FROM schema_migrations")
    store._conn.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
        ("2026-08-29T00:00:00+00:00",),
    )
    store.close()

    with pytest.raises(PersistenceError) as info:
        SQLiteStore(path)
    message = str(info.value)
    assert "unsupported schema version 1" in message

    text = "\n".join(p.read_text(encoding="utf-8") for p in (PKG / "persistence").rglob("*.py"))
    assert "ALTER TABLE" not in text, "probe assumption: no migration statements"
    pytest.fail(
        "SCHEMA V2: FRESH-DB REQUIRED. A v1 database fails closed on open "
        f"({message!r}) and the package contains no ALTER TABLE and no migration "
        "step - only a version equality check. Failing closed is the right default, "
        "but any existing v1 state (proposals, reservations, audit history) is "
        "unreachable without an explicit migration."
    )


def test_FINDING_no_check_binds_reference_price_to_the_price_mapping(tmp_path):
    """The mechanism behind the blocker: a leg's reference_price and the market
    price mapping are two independent inputs, and nothing requires them to agree.
    Authority and funding are measured with one; concentration with the other."""
    from opaca.domain.models import Side
    from opaca.policy.engine import CHECK_ORDER

    from p3_support import make_order, make_proposal

    w = world(tmp_path, qty="0", cash="500000")
    absurd = make_proposal(
        "b1", [make_order("b1", 0, "SGOV", Side.BUY, "100", Decimal("0.01"))]
    )
    _, out = reserve(w, absurd)
    decision = out.decision
    failed = (
        [] if decision is None
        else [r.check_id.value for r in decision.policy_decision.results if not r.passed]
    )
    notional = absurd.total_buy_notional
    w.close()
    assert notional == Decimal("1.00")
    assert not any(
        "reference" in check.value.lower() for check in CHECK_ORDER
    ), "probe assumption: no dedicated reference-price check exists"
    pytest.fail(
        "PRE-LIVE BLOCKER (mechanism): a leg priced at 0.01 against a market price of "
        f"{SGOV} yields a notional of {notional} and fails only {failed or 'nothing'} - "
        "no check compares a leg's reference_price with context.prices[symbol]. "
        "Authority, funding and dust checks use reference_price; concentration uses "
        "the price mapping. Both must come from a real quote before a live order."
    )
