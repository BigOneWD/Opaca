"""P2-2 retest: the market-data adapter tests are a real gate, not a silent skip."""
from __future__ import annotations

import ast
import os
import pathlib

BACKEND = pathlib.Path(os.environ["OPACA_BACKEND"])
TESTS = BACKEND / "tests"


def test_pytz_is_pinned_in_the_dev_gate_requirements():
    text = (BACKEND / "requirements-dev.txt").read_text(encoding="utf-8")
    pins = [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]
    assert any(p.startswith("pytz==") for p in pins), pins
    assert any(p.startswith("alpaca-py==") for p in pins), pins


def test_pytz_is_pinned_in_the_paper_extra():
    text = (BACKEND / "pyproject.toml").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("paper ="))
    assert "pytz==" in line, line
    assert "alpaca-py==" in line, line


def test_no_market_data_test_is_gated_behind_an_import_or_skip():
    offenders = []
    for path in TESTS.rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "importorskip"):
                offenders.append(f"{path.name}:{node.lineno} {ast.unparse(node)}")
    assert offenders == [], offenders


def test_the_canonical_adapter_and_feed_assertions_exist_and_are_unconditional():
    src = (TESTS / "test_market_price.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "test_iex_feed_is_requested" in names
    assert "test_inclusive_fifteen_second_boundary" in names
    assert "DataFeed.IEX" in src
    assert "importorskip" not in src
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "TestAlpacaAdapterFailClosed")
    decorated = [f.name for f in cls.body
                 if isinstance(f, ast.FunctionDef) and f.decorator_list]
    assert decorated == [] or all("fixture" not in ast.unparse(d)
                                  for f in cls.body if isinstance(f, ast.FunctionDef)
                                  for d in f.decorator_list), decorated


def test_the_offline_suite_declares_only_the_three_live_opt_in_skips():
    """Every skip in the suite is declared in one place and is a live opt-in."""
    conf = (TESTS / "conftest.py").read_text(encoding="utf-8")
    tree = ast.parse(conf)
    marks = [ast.unparse(n) for n in ast.walk(tree)
             if isinstance(n, ast.Call) and "pytest.mark.skip" in ast.unparse(n)]
    assert len(marks) == 3, marks
    assert all("live paper" in m for m in marks), marks
    offenders = []
    for path in TESTS.rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            text = ast.unparse(node)
            if ("pytest.skip(" in text or "importorskip" in text
                    or "mark.skipif" in text or "mark.skip(" in text):
                offenders.append(f"{path.name}:{node.lineno} {text[:90]}")
    assert offenders == [], offenders
