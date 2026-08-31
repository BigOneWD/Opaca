"""S7: read-only preflight. It must never mutate broker state."""
from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys
from datetime import timedelta
from decimal import Decimal

import pytest
from opaca.broker.errors import PaperEnvironmentError
from opaca.broker.gateway import LIVE_ENDPOINT, PAPER_ENDPOINT
from opaca.market.source import FakeMarketData
from opaca.persistence.demo import PAPER_DEMO_DB_NAME
from opaca.preflight import (
    EXECUTION_NOT_ATTEMPTED,
    credentials_present,
    run_read_only_preflight,
)

from support import DEFAULT_NOW, quotes_for, world

BACKEND = pathlib.Path(os.environ["OPACA_BACKEND"])
PKG = BACKEND / "opaca"

MUTATORS = {
    "submit_order", "cancel_order_by_id", "cancel_order", "close_position",
    "close_all_positions", "replace_order", "delete_order", "cancel_orders",
    "liquidate", "evaluate_and_reserve", "execute_reserved_proposal",
    "persist_reservations", "insert_execution_order", "grant_human_approval",
}


class Watchdog:
    """Wraps the read gateway; explodes if anything mutating is touched."""

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "reads", [])

    def __getattr__(self, name):
        if name in MUTATORS:
            # absent, exactly as a read-only gateway must be; introspection by
            # assert_read_only_gateway is expected and must not trip the trap
            raise AttributeError(name)
        attr = getattr(self._inner, name)
        if callable(attr):
            def traced(*a, **kw):
                self.reads.append(name)
                return attr(*a, **kw)
            return traced
        return attr


def _world(tmp_path, name, **kw):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return world(d, **kw)


def _run(tmp_path, w, *, quotes=None, gateway=None, now=DEFAULT_NOW, **kw):
    market = FakeMarketData(quotes=quotes if quotes is not None else quotes_for(now=now))
    gw = gateway if gateway is not None else w.read()
    return run_read_only_preflight(
        gw, market, now=now, db_path=tmp_path / PAPER_DEMO_DB_NAME, **kw)


def test_preflight_reads_only_and_reports_execution_not_attempted(tmp_path):
    w = _world(tmp_path, "w", qty="0", cash="100000")
    dog = Watchdog(w.read())
    report = _run(tmp_path, w, gateway=dog)
    w.close()
    assert report.ran is True
    assert report.execution == EXECUTION_NOT_ATTEMPTED
    assert report.paper_account == "ACTIVE"
    assert set(dog.reads) <= {
        "get_account", "get_positions", "get_asset", "get_clock", "get_calendar",
        "get_open_orders", "get_order_by_client_id",
    }, dog.reads
    assert "get_account" in dog.reads and "get_clock" in dog.reads


def test_preflight_creates_no_reservation_intent_or_execution_row(tmp_path):
    w = _world(tmp_path, "w2", qty="0", cash="100000")
    report = _run(tmp_path, w)
    w.close()
    from opaca.persistence.demo import open_existing_paper_demo_store
    store = open_existing_paper_demo_store(tmp_path / PAPER_DEMO_DB_NAME)
    try:
        assert store.list_execution_orders() == ()
        assert [r for r in store.active_reservations()] == []
        assert store.get_proposal("preflight-sgov-buy-1") is None
    finally:
        store.close()
    assert report.execution == EXECUTION_NOT_ATTEMPTED


def test_preflight_source_never_names_a_mutating_call():
    tree = ast.parse((PKG / "preflight.py").read_text(encoding="utf-8"))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {a.name for a in node.names}
    assert not (names & MUTATORS), sorted(names & MUTATORS)
    assert "PaperMutatingGateway" not in names
    assert "open_paper_execution_gateway_from_env" not in names


def test_a_live_endpoint_fails_closed(tmp_path):
    w = _world(tmp_path, "w3", qty="0", cash="100000")
    gw = w.read()
    object.__setattr__(gw, "endpoint", LIVE_ENDPOINT)
    report = _run(tmp_path, w, gateway=gw)
    w.close()
    assert report.paper_account == "FAIL"
    assert report.execution == EXECUTION_NOT_ATTEMPTED
    assert "live" in (report.fail_reason or "").lower()


@pytest.mark.parametrize("endpoint", ["", "https://api.alpaca.markets",
                                      "http://localhost:8080",
                                      "https://evil.example.com",
                                      "https://api.alpaca.markets/v2"])
def test_any_non_paper_endpoint_fails_closed(tmp_path, endpoint):
    w = _world(tmp_path, f"e{abs(hash(endpoint))}", qty="0", cash="100000")
    gw = w.read()
    object.__setattr__(gw, "endpoint", endpoint)
    report = _run(tmp_path, w, gateway=gw)
    w.close()
    assert report.paper_account == "FAIL"
    assert report.execution == EXECUTION_NOT_ATTEMPTED


def test_FINDING_a_look_alike_paper_host_satisfies_every_endpoint_guard(tmp_path):
    """All four endpoint guards are prefix tests on
    'https://paper-api.alpaca.markets', which an attacker-controlled
    'https://paper-api.alpaca.markets.evil.com' satisfies."""
    from opaca.execution.gateway import assert_paper_execution_gateway
    from opaca.broker.paper import verify_paper_client

    lookalike = PAPER_ENDPOINT + ".evil.com"
    w = _world(tmp_path, "lookalike", qty="0", cash="100000")
    read_gw = w.read()
    object.__setattr__(read_gw, "endpoint", lookalike)
    report = _run(tmp_path, w, gateway=read_gw)

    mutate_gw = w.mutate()
    mutate_gw.endpoint = lookalike
    accepted_mutate = True
    try:
        assert_paper_execution_gateway(mutate_gw)
    except PaperEnvironmentError:
        accepted_mutate = False

    class Client:
        _base_url = lookalike
        _paper = True

    accepted_client = True
    try:
        verify_paper_client(Client())
    except PaperEnvironmentError:
        accepted_client = False
    w.close()

    assert not (report.paper_account == "ACTIVE" and accepted_mutate
                and accepted_client), (
        f"{lookalike!r} was accepted as the paper endpoint by preflight "
        f"(paper_account={report.paper_account}), by "
        "assert_paper_execution_gateway and by verify_paper_client. Every "
        "guard is `startswith(PAPER_ENDPOINT)`; none anchors the host."
    )


def test_a_stale_quote_makes_preflight_fail_without_touching_the_db(tmp_path):
    w = _world(tmp_path, "w4", qty="0", cash="100000")
    report = _run(tmp_path, w, quotes=quotes_for(now=DEFAULT_NOW, age_seconds=60))
    w.close()
    assert report.fail_reason and "exceeds max" in report.fail_reason
    assert report.execution == EXECUTION_NOT_ATTEMPTED
    assert not (tmp_path / PAPER_DEMO_DB_NAME).exists()


def test_unavailable_market_data_makes_preflight_fail_closed(tmp_path):
    w = _world(tmp_path, "w5", qty="0", cash="100000")
    market = FakeMarketData(unavailable=True)
    report = run_read_only_preflight(
        w.read(), market, now=DEFAULT_NOW, db_path=tmp_path / PAPER_DEMO_DB_NAME)
    w.close()
    assert report.execution == EXECUTION_NOT_ATTEMPTED
    assert report.fail_reason


def test_preflight_refuses_to_silently_overwrite_the_demo_db(tmp_path):
    w = _world(tmp_path, "w6", qty="0", cash="100000")
    first = _run(tmp_path, w)
    assert first.fail_reason is None or "refus" not in first.fail_reason
    second = _run(tmp_path, w)
    w.close()
    assert second.execution == EXECUTION_NOT_ATTEMPTED
    assert "refusing to overwrite" in (second.fail_reason or "")


def test_the_report_never_prints_credentials(tmp_path, monkeypatch):
    from opaca.broker.paper import ENV_KEY_ID, ENV_SECRET
    monkeypatch.setenv(ENV_KEY_ID, "PKREDTEAMKEY0000000")
    monkeypatch.setenv(ENV_SECRET, "sekritsekritsekritsekrit")
    assert credentials_present() is True
    w = _world(tmp_path, "w7", qty="0", cash="100000")
    text = _run(tmp_path, w).render()
    w.close()
    assert "PKREDTEAMKEY0000000" not in text
    assert "sekritsekritsekritsekrit" not in text
    assert ENV_KEY_ID not in text and ENV_SECRET not in text


def test_the_cli_without_credentials_reports_not_run_and_never_dials(tmp_path):
    env = dict(os.environ)
    env.pop("APCA_API_KEY_ID", None)
    env.pop("APCA_API_SECRET_KEY", None)
    env["PYTHONPATH"] = str(BACKEND)
    proc = subprocess.run(
        [sys.executable, "-m", "opaca", "preflight", "--db",
         str(tmp_path / PAPER_DEMO_DB_NAME)],
        capture_output=True, text=True, env=env, cwd=str(BACKEND), timeout=60)
    assert proc.returncode == 2
    assert "READ-ONLY PREFLIGHT: NOT RUN" in proc.stdout
    assert EXECUTION_NOT_ATTEMPTED in proc.stdout
    assert not (tmp_path / PAPER_DEMO_DB_NAME).exists()


def test_the_cli_help_advertises_only_preflight(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND)
    proc = subprocess.run([sys.executable, "-m", "opaca", "--help"],
                          capture_output=True, text=True, env=env,
                          cwd=str(BACKEND), timeout=60)
    assert proc.returncode == 0
    assert "preflight" in proc.stdout
    assert "never submits or cancels" in proc.stdout
    for word in ("execute", "trade", "buy ", "sell "):
        assert word not in proc.stdout.lower()
    bad = subprocess.run([sys.executable, "-m", "opaca", "execute"],
                         capture_output=True, text=True, env=env,
                         cwd=str(BACKEND), timeout=60)
    assert bad.returncode == 2


def test_a_passing_preflight_is_not_execution_authority():
    doc = (PKG / "preflight.py").read_text(encoding="utf-8")
    assert "not execution authority" in doc
    assert "No stale preflight result may authorize a trade." in doc
