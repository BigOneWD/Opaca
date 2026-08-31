"""Fresh schema-v2 paper-demo database. Never a test DB. Never auto-deleted.

Reset procedure (manual only):

1. Stop any process using the file.
2. Delete ``opaca-paper-demo.db``, ``opaca-paper-demo.db-wal``, and
   ``opaca-paper-demo.db-shm`` explicitly.
3. Re-run ``init_paper_demo_store`` (or ``python -m opaca preflight``).

This helper refuses to overwrite an existing file unless ``overwrite=True``.
It never migrates a v1 file in place. Test fixtures that use
``tmp_path / "opaca.sqlite"`` are a different path and a different role.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from opaca.persistence.schema import SCHEMA_VERSION
from opaca.persistence.store import PersistenceError, SQLiteStore

PAPER_DEMO_DB_NAME = "opaca-paper-demo.db"
PAPER_DEMO_DB_ROLE = "paper-demo"
DEFAULT_DEMO_OPENING_CASH = Decimal("100000")
_FORBIDDEN_DEMO_FILENAMES = frozenset({"opaca.sqlite", "live-paper.sqlite"})


class DemoDatabaseError(PersistenceError):
    """Demo DB path/overwrite/schema contract violated. Fail closed."""


def _unlink_sqlite(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists() and candidate.is_file():
            candidate.unlink()


def init_paper_demo_store(
    path: str | Path,
    *,
    now: datetime,
    seeded_at: date | None = None,
    opening_cash: Decimal = DEFAULT_DEMO_OPENING_CASH,
    overwrite: bool = False,
    timeout: float = 5.0,
) -> SQLiteStore:
    """Create a dedicated schema-v2 paper-demo DB, seed once, verify WAL+FK.

    Existing files are refused unless ``overwrite=True``. Overwrite unlinks
    the file (and WAL/SHM) then bootstraps a new schema-v2 store. It does
    not migrate v1. It does not auto-delete without the flag.
    """
    target = Path(path)
    if str(target) == ":memory:":
        raise DemoDatabaseError("paper-demo DB cannot be :memory: (WAL required)")
    if target.name in _FORBIDDEN_DEMO_FILENAMES:
        raise DemoDatabaseError(
            f"{target.name} is a test/live-smoke filename; use {PAPER_DEMO_DB_NAME}"
        )
    if target.exists():
        if not overwrite:
            raise DemoDatabaseError(
                f"refusing to overwrite existing DB {target}; "
                "pass overwrite=True or delete the file explicitly"
            )
        _unlink_sqlite(target)
    seed_date = now.date() if seeded_at is None else seeded_at
    store = SQLiteStore(target, timeout=timeout)
    try:
        _verify_fresh_v2(store)
        with store.begin_immediate() as conn:
            store.upsert_system_value("db_role", PAPER_DEMO_DB_ROLE, now=now, conn=conn)
            store.seed_scenario_once(opening_cash, seed_date, now=now, conn=conn)
        _verify_fresh_v2(store)
        if store.system_value("db_role") != PAPER_DEMO_DB_ROLE:
            raise DemoDatabaseError("paper-demo db_role marker missing")
        if store.get_scenario() is None:
            raise DemoDatabaseError("paper-demo scenario seed missing")
    except Exception:
        store.close()
        raise
    return store


def open_existing_paper_demo_store(path: str | Path, *, timeout: float = 5.0) -> SQLiteStore:
    """Open a previously initialized paper-demo DB. Does not seed or overwrite."""
    target = Path(path)
    if not target.exists():
        raise DemoDatabaseError(f"paper-demo DB does not exist: {target}")
    store = SQLiteStore(target, timeout=timeout)
    try:
        _verify_fresh_v2(store)
        if store.system_value("db_role") != PAPER_DEMO_DB_ROLE:
            raise DemoDatabaseError(
                f"{target} is not a paper-demo DB (db_role mismatch); refuse reuse"
            )
    except Exception:
        store.close()
        raise
    return store


def _verify_fresh_v2(store: SQLiteStore) -> None:
    if store.path == ":memory:":
        raise DemoDatabaseError("paper-demo DB cannot be :memory:")
    version = store.schema_version()
    if version != SCHEMA_VERSION:
        raise DemoDatabaseError(
            f"unsupported schema version {version}; expected {SCHEMA_VERSION} (fail closed)"
        )
    if store.journal_mode() != "WAL":
        raise DemoDatabaseError("paper-demo DB is not in WAL mode")
    if not store.foreign_keys_enabled():
        raise DemoDatabaseError("paper-demo DB does not have foreign_keys ON")
