from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live-paper",
        action="store_true",
        default=False,
        help="run optional live PAPER read-only smoke tests",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live_paper: optional live PAPER read-only smoke (requires credentials)"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--live-paper"):
        return
    skip = pytest.mark.skip(reason="live paper smoke not requested")
    for item in items:
        if "live_paper" in item.keywords:
            item.add_marker(skip)
