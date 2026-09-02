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
from opaca.wheel.models import WheelShareLot


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
    kind: str = "CASH_DEPLOYMENT"


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
        status TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'CASH_DEPLOYMENT'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wheel_share_lots (
        lot_id TEXT PRIMARY KEY,
        underlying TEXT NOT NULL,
        shares TEXT NOT NULL,
        cost_basis TEXT NOT NULL,
        market_value TEXT NOT NULL DEFAULT '0'
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
            self._ensure_column(
                connection,
                "wheel_reservations",
                "kind",
                "TEXT NOT NULL DEFAULT 'CASH_DEPLOYMENT'",
            )
            self._ensure_column(
                connection,
                "wheel_share_lots",
                "market_value",
                "TEXT NOT NULL DEFAULT '0'",
            )

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
            "SELECT reservation_id, underlying, amount, status, kind "
            "FROM wheel_reservations WHERE status = ? AND kind = ? ORDER BY reservation_id",
            ("ACTIVE", "CASH_DEPLOYMENT"),
        ).fetchall()
        return [
            WheelReservation(
                reservation_id=str(row["reservation_id"]),
                underlying=str(row["underlying"]),
                amount=load_decimal(str(row["amount"])),
                status=str(row["status"]),
                kind=str(row["kind"]),
            )
            for row in rows
        ]

    def reserve_assignment(
        self,
        *,
        reservation_id: str,
        underlying: str,
        amount: Decimal,
        now: datetime,
    ) -> WheelReservation:
        """Create one idempotent ACTIVE cash-deployment reservation."""
        if not reservation_id.strip() or not underlying.strip():
            raise ValueError("reservation identity must be non-empty")
        capital = positive_money(amount)
        dump_datetime(now)
        with self.begin_immediate() as connection:
            existing = connection.execute(
                "SELECT reservation_id, underlying, amount, status, kind "
                "FROM wheel_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if existing is not None:
                existing_amount = load_decimal(str(existing["amount"]))
                if (
                    str(existing["underlying"]) != underlying
                    or existing_amount != capital
                    or str(existing["kind"]) != "CASH_DEPLOYMENT"
                ):
                    raise WheelPersistenceError("reservation identity is already bound")
                if str(existing["status"]) != "ACTIVE":
                    raise WheelPersistenceError("reservation is not active")
                return WheelReservation(
                    reservation_id=str(existing["reservation_id"]),
                    underlying=str(existing["underlying"]),
                    amount=existing_amount,
                    status=str(existing["status"]),
                    kind=str(existing["kind"]),
                )
            connection.execute(
                "INSERT INTO wheel_reservations "
                "(reservation_id, underlying, amount, status, kind) "
                "VALUES (?, ?, ?, ?, ?)",
                (reservation_id, underlying, dump_decimal(capital), "ACTIVE", "CASH_DEPLOYMENT"),
            )
        return WheelReservation(
            reservation_id=reservation_id,
            underlying=underlying,
            amount=capital,
            status="ACTIVE",
        )

    def release_assignment_if_proven_no_exposure(
        self,
        reservation_id: str,
        *,
        proven: bool,
        now: datetime,
    ) -> bool:
        """Release only when a caller supplies a positive no-exposure proof."""
        dump_datetime(now)
        if not proven:
            return False
        with self.begin_immediate() as connection:
            cursor = connection.execute(
                "UPDATE wheel_reservations SET status = ? "
                "WHERE reservation_id = ? AND kind = ? AND status = ?",
                ("RELEASED", reservation_id, "CASH_DEPLOYMENT", "ACTIVE"),
            )
            return cursor.rowcount == 1

    def convert_assignment_to_share_lot(
        self,
        *,
        reservation_id: str,
        lot_id: str,
        underlying: str,
        shares: int,
        assignment_basis: Decimal,
        market_value: Decimal,
        now: datetime,
    ) -> None:
        """Atomically persist assigned shares before releasing their reservation."""
        lot = WheelShareLot(
            underlying=underlying,
            shares=shares,
            assignment_basis=assignment_basis,
            market_value=market_value,
        )
        dump_datetime(now)
        basis = lot.assignment_basis.quantize(Decimal("0.01"))
        if basis != lot.assignment_basis:
            raise ValueError("assignment basis must be cent-exact")
        with self.begin_immediate() as connection:
            reservation = connection.execute(
                "SELECT underlying, status, kind FROM wheel_reservations "
                "WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if reservation is None:
                raise WheelPersistenceError("assignment reservation is missing")
            if (
                str(reservation["underlying"]) != lot.underlying
                or str(reservation["status"]) != "ACTIVE"
                or str(reservation["kind"]) != "CASH_DEPLOYMENT"
            ):
                raise WheelPersistenceError("assignment reservation is not active")
            connection.execute(
                "INSERT INTO wheel_share_lots "
                "(lot_id, underlying, shares, cost_basis, market_value) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(lot_id) DO UPDATE SET "
                "underlying = excluded.underlying, shares = excluded.shares, "
                "cost_basis = excluded.cost_basis, market_value = excluded.market_value",
                (
                    lot_id,
                    lot.underlying,
                    str(lot.shares),
                    format(basis, "f"),
                    dump_decimal(lot.market_value),
                ),
            )
            cursor = connection.execute(
                "UPDATE wheel_reservations SET status = ? "
                "WHERE reservation_id = ? AND status = ?",
                ("RELEASED", reservation_id, "ACTIVE"),
            )
            if cursor.rowcount != 1:
                raise WheelPersistenceError("assignment reservation release failed")

    def share_lots(self) -> list[WheelShareLot]:
        """Load attributable Wheel share lots for exposure calculation."""
        rows = self._conn.execute(
            "SELECT underlying, shares, cost_basis, market_value "
            "FROM wheel_share_lots ORDER BY lot_id"
        ).fetchall()
        return [
            WheelShareLot(
                underlying=str(row["underlying"]),
                shares=int(str(row["shares"])),
                assignment_basis=load_decimal(str(row["cost_basis"])),
                market_value=load_decimal(str(row["market_value"])),
            )
            for row in rows
        ]

    @staticmethod
    def _meta_value(connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute("SELECT value FROM wheel_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return str(row["value"])

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
        if column not in {str(row[1]) for row in columns}:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
