"""Isolated persistence for the V1 Competition Wheel bootstrap state."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from opaca.domain.money import positive_money
from opaca.persistence.codec import dump_datetime, dump_decimal, load_decimal


class WheelPersistenceError(RuntimeError):
    """Fail-closed error raised by the isolated Wheel store."""


class WheelAccountMismatchError(WheelPersistenceError):
    """The supplied broker account does not match the bound Wheel account."""


@dataclass(frozen=True)
class WheelAccountBinding:
    """The non-sensitive account binding and immutable capital seed."""

    account_fingerprint: str
    risk_capital_base: Decimal
    bound_at: datetime


@dataclass(frozen=True)
class WheelReservation:
    """Minimal structural representation of a Wheel reservation."""

    reservation_id: str
    underlying: str
    amount: Decimal
    status: str


@dataclass(frozen=True)
class WheelOrderRecord:
    """Minimal structural representation of a Wheel order."""

    client_order_id: str
    occ_symbol: str
    status: str


@dataclass(frozen=True)
class WheelAuditEvent:
    """Minimal structural representation of a Wheel audit event."""

    event_type: str
    occurred_at: datetime
    detail: str


_ACCOUNT_FINGERPRINT_KEY = "account_fingerprint"
_RISK_CAPITAL_BASE_KEY = "risk_capital_base"
_ACCOUNT_BOUND_AT_KEY = "account_bound_at"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS wheel_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wheel_reservations (
        reservation_id TEXT PRIMARY KEY,
        underlying TEXT NOT NULL,
        amount TEXT NOT NULL,
        status TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wheel_share_lots (
        lot_id TEXT PRIMARY KEY,
        underlying TEXT NOT NULL,
        shares TEXT NOT NULL,
        cost_basis TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wheel_orders (
        client_order_id TEXT PRIMARY KEY,
        occ_symbol TEXT NOT NULL,
        status TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wheel_approvals (
        approval_id TEXT PRIMARY KEY,
        decision_run_id TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wheel_audit (
        audit_id INTEGER PRIMARY KEY,
        event_type TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        detail TEXT NOT NULL
    )
    """,
)


def _validated_account_id(account_id: str) -> str:
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("account_id must be a non-empty string")
    return account_id


def _account_fingerprint(account_id: str) -> str:
    return hashlib.sha256(_validated_account_id(account_id).encode("utf-8")).hexdigest()


class WheelStore:
    """A dedicated SQLite database for Competition Wheel state."""

    def __init__(self, path: str | Path, *, timeout: float = 5.0) -> None:
        self.path = str(path)
        self.timeout = timeout
        self._conn = sqlite3.connect(
            self.path,
            timeout=self.timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self.begin_immediate() as connection:
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> WheelStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def begin_immediate(self) -> Iterator[sqlite3.Connection]:
        """Run a transaction using SQLite's single-writer lock."""
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise WheelPersistenceError("unable to begin Wheel transaction") from exc
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def bootstrap_account(
        self,
        account_id: str,
        opening_cash: Decimal,
        now: datetime,
    ) -> None:
        """Bind this database once and seed its immutable risk capital."""
        fingerprint = _account_fingerprint(account_id)
        capital = positive_money(opening_cash)
        bound_at = dump_datetime(now)

        with self.begin_immediate() as connection:
            stored_fingerprint = self._meta_value(connection, _ACCOUNT_FINGERPRINT_KEY)
            stored_capital = self._meta_value(connection, _RISK_CAPITAL_BASE_KEY)
            stored_bound_at = self._meta_value(connection, _ACCOUNT_BOUND_AT_KEY)

            if stored_fingerprint is not None:
                if stored_fingerprint != fingerprint:
                    raise WheelAccountMismatchError("Wheel account binding mismatch")
                if stored_capital is None or stored_bound_at is None:
                    raise WheelPersistenceError("incomplete Wheel account binding")
                return

            if stored_capital is not None or stored_bound_at is not None:
                raise WheelPersistenceError("incomplete Wheel account binding")

            connection.executemany(
                "INSERT INTO wheel_meta(key, value) VALUES (?, ?)",
                (
                    (_ACCOUNT_FINGERPRINT_KEY, fingerprint),
                    (_RISK_CAPITAL_BASE_KEY, dump_decimal(capital)),
                    (_ACCOUNT_BOUND_AT_KEY, bound_at),
                ),
            )

    def assert_account_binding(self, account_id: str) -> bool | None:
        """Require the supplied account to match the persisted fingerprint."""
        fingerprint = _account_fingerprint(account_id)
        stored_fingerprint = self._meta_value(self._conn, _ACCOUNT_FINGERPRINT_KEY)
        if stored_fingerprint != fingerprint:
            raise WheelAccountMismatchError("Wheel account binding mismatch")
        return None

    def risk_capital_base(self) -> Decimal:
        """Return the immutable capital value seeded at account bootstrap."""
        stored_capital = self._meta_value(self._conn, _RISK_CAPITAL_BASE_KEY)
        if stored_capital is None:
            raise WheelPersistenceError("Wheel risk capital is not initialized")
        return load_decimal(stored_capital)

    def active_assignment_reservations(self) -> list[WheelReservation]:
        """Return active assignment reservations from this Wheel database."""
        rows = self._conn.execute(
            "SELECT reservation_id, underlying, amount, status "
            "FROM wheel_reservations WHERE status = ? ORDER BY reservation_id",
            ("ACTIVE",),
        ).fetchall()
        return [
            WheelReservation(
                reservation_id=str(row["reservation_id"]),
                underlying=str(row["underlying"]),
                amount=load_decimal(str(row["amount"])),
                status=str(row["status"]),
            )
            for row in rows
        ]

    @staticmethod
    def _meta_value(connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute("SELECT value FROM wheel_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return str(row["value"])
