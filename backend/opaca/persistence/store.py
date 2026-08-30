"""Single-writer SQLite store. WAL, foreign_keys, explicit transactions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from opaca.domain.models import (
    AssetState,
    AssetStatus,
    AuthorityDecision,
    AuthorityResult,
    AutonomousExecution,
    BrokerCashState,
    Obligation,
    Position,
    Proposal,
    ProposedOrder,
    SettlementEvent,
)
from opaca.persistence.codec import (
    dump_date,
    dump_datetime,
    dump_decimal,
    load_date,
    load_datetime,
    load_decimal,
)
from opaca.persistence.schema import (
    DEFAULT_POLICY_ROWS,
    DEFAULT_SYSTEM_STATE,
    SCHEMA_STATEMENTS,
    SCHEMA_VERSION,
)
from opaca.persistence.types import (
    AuditEvent,
    AuditEventType,
    LocalLedger,
    OrderSnapshotRecord,
    PersistedSnapshot,
    ProposalRecord,
    ProposalRecordStatus,
    ReconciliationStatus,
    ReservationKind,
    ReservationRecord,
    ReservationStatus,
    UnknownOrderRecord,
)
from opaca.treasury.scenario import ScenarioSeed, seed_scenario


class PersistenceError(RuntimeError):
    """Fail-closed persistence error."""


class StaleSnapshotError(PersistenceError):
    """The caller's snapshot version is not the latest reconciled version."""


class SqliteBusyError(PersistenceError):
    """SQLite was locked/busy; fail closed rather than guess."""


def _now_utc() -> datetime:
    return datetime.now(UTC)


class SQLiteStore:
    """Authoritative local state. One logical writer; BEGIN IMMEDIATE."""

    def __init__(self, path: str | Path, *, timeout: float = 5.0) -> None:
        self.path = str(path)
        self.timeout = timeout
        self._conn = self._connect()
        self.bootstrap()

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                self.path,
                timeout=self.timeout,
                isolation_level=None,
                check_same_thread=False,
            )
        except sqlite3.OperationalError as exc:
            raise SqliteBusyError(str(exc)) from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def connect_writer(self) -> sqlite3.Connection:
        """Additional writer connection to the same file (concurrency tests)."""
        if self.path == ":memory:":
            raise PersistenceError("cannot open a second connection to an anonymous memory DB")
        conn = sqlite3.connect(
            self.path,
            timeout=self.timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        return conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def bootstrap(self) -> None:
        existing = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if existing is None:
            self._apply_schema(self._conn)
            return
        row = self._conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        version = int(row["v"]) if row is not None and row["v"] is not None else 0
        if version != SCHEMA_VERSION:
            raise PersistenceError(
                f"unsupported schema version {version}; expected {SCHEMA_VERSION} (fail closed)"
            )

    def _apply_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            applied = dump_datetime(_now_utc())
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, applied),
            )
            for name, value, value_type in DEFAULT_POLICY_ROWS:
                conn.execute(
                    "INSERT INTO policies(name, value, value_type, updated_at) VALUES (?, ?, ?, ?)",
                    (name, value, value_type, applied),
                )
            for key, value in DEFAULT_SYSTEM_STATE:
                conn.execute(
                    "INSERT INTO system_state(key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, applied),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    @contextmanager
    def begin_immediate(
        self, conn: sqlite3.Connection | None = None
    ) -> Iterator[sqlite3.Connection]:
        target = conn if conn is not None else self._conn
        try:
            target.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise SqliteBusyError(str(exc)) from exc
        try:
            yield target
        except Exception:
            target.execute("ROLLBACK")
            raise
        else:
            target.execute("COMMIT")

    def journal_mode(self) -> str:
        row = self._conn.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).upper()

    def foreign_keys_enabled(self) -> bool:
        row = self._conn.execute("PRAGMA foreign_keys").fetchone()
        return int(row[0]) == 1

    def schema_version(self) -> int:
        row = self._conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        return int(row["v"])

    def policy_value(self, name: str, conn: sqlite3.Connection | None = None) -> str:
        target = conn if conn is not None else self._conn
        row = target.execute("SELECT value FROM policies WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise PersistenceError(f"missing policy {name!r} (fail closed)")
        return str(row["value"])

    def kill_switch_active(self, conn: sqlite3.Connection | None = None) -> bool:
        target = conn if conn is not None else self._conn
        row = target.execute("SELECT value FROM system_state WHERE key = 'kill_switch'").fetchone()
        return row is not None and str(row["value"]) == "1"

    def set_kill_switch(self, active: bool, *, now: datetime) -> None:
        self._conn.execute(
            "UPDATE system_state SET value = ?, updated_at = ? WHERE key = 'kill_switch'",
            ("1" if active else "0", dump_datetime(now)),
        )

    def get_scenario(self, conn: sqlite3.Connection | None = None) -> ScenarioSeed | None:
        target = conn if conn is not None else self._conn
        row = target.execute(
            "SELECT opening_cash, seeded_at, operating_reserve, investable_surplus "
            "FROM scenario_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        obligations = self.load_obligations(conn=target)
        return ScenarioSeed(
            opening_cash=load_decimal(str(row["opening_cash"])),
            seeded_at=load_date(str(row["seeded_at"])),
            obligations=obligations,
            operating_reserve=load_decimal(str(row["operating_reserve"])),
            investable_surplus=load_decimal(str(row["investable_surplus"])),
        )

    def seed_scenario_once(
        self,
        opening_cash: Decimal,
        seeded_at: date,
        *,
        now: datetime,
        conn: sqlite3.Connection | None = None,
    ) -> ScenarioSeed:
        """Persist the ratio-to-absolute seed once. Later cash must not rescale.

        Direct calls (conn is None) run under BEGIN IMMEDIATE so a mid-seed
        failure rolls back scenario_state together with obligations.
        """
        if conn is None:
            with self.begin_immediate() as txn:
                return self.seed_scenario_once(opening_cash, seeded_at, now=now, conn=txn)
        target = conn
        existing = self.get_scenario(conn=target)
        if existing is not None:
            return existing
        seed = seed_scenario(opening_cash, seeded_at)
        try:
            target.execute(
                "INSERT INTO scenario_state("
                "id, opening_cash, seeded_at, operating_reserve, investable_surplus, created_at"
                ") VALUES (1, ?, ?, ?, ?, ?)",
                (
                    dump_decimal(seed.opening_cash),
                    dump_date(seed.seeded_at),
                    dump_decimal(seed.operating_reserve),
                    dump_decimal(seed.investable_surplus),
                    dump_datetime(now),
                ),
            )
        except sqlite3.IntegrityError:
            raced = self.get_scenario(conn=target)
            if raced is not None:
                return raced
            raise
        for obligation in seed.obligations:
            target.execute(
                "INSERT INTO obligations("
                "obligation_id, name, amount, due_date, seeded"
                ") VALUES (?, ?, ?, ?, 1)",
                (
                    obligation.obligation_id,
                    obligation.name,
                    dump_decimal(obligation.amount),
                    dump_date(obligation.due_date),
                ),
            )
        self.record_audit(
            AuditEventType.SCENARIO_SEEDED,
            now,
            reason="scenario seeded once from reconciled opening broker cash",
            detail=json.dumps(
                {
                    "opening_cash": dump_decimal(seed.opening_cash),
                    "operating_reserve": dump_decimal(seed.operating_reserve),
                },
                separators=(",", ":"),
            ),
            conn=target,
        )
        return seed

    def load_obligations(self, conn: sqlite3.Connection | None = None) -> tuple[Obligation, ...]:
        target = conn if conn is not None else self._conn
        rows = target.execute(
            "SELECT obligation_id, name, amount, due_date FROM obligations ORDER BY obligation_id"
        ).fetchall()
        return tuple(
            Obligation(
                obligation_id=str(row["obligation_id"]),
                name=str(row["name"]),
                amount=load_decimal(str(row["amount"])),
                due_date=load_date(str(row["due_date"])),
            )
            for row in rows
        )

    def insert_settlement_event(
        self, event: SettlementEvent, *, now: datetime, conn: sqlite3.Connection | None = None
    ) -> None:
        target = conn if conn is not None else self._conn
        target.execute(
            "INSERT INTO settlement_events("
            "event_id, symbol, trade_date, settlement_date, amount, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.symbol,
                dump_date(event.trade_date),
                dump_date(event.settlement_date),
                dump_decimal(event.amount),
                dump_datetime(now),
            ),
        )

    def load_settlement_events(
        self, conn: sqlite3.Connection | None = None
    ) -> tuple[SettlementEvent, ...]:
        target = conn if conn is not None else self._conn
        rows = target.execute(
            "SELECT event_id, symbol, trade_date, settlement_date, amount "
            "FROM settlement_events ORDER BY event_id"
        ).fetchall()
        return tuple(
            SettlementEvent(
                event_id=str(row["event_id"]),
                symbol=str(row["symbol"]),
                trade_date=load_date(str(row["trade_date"])),
                settlement_date=load_date(str(row["settlement_date"])),
                amount=load_decimal(str(row["amount"])),
            )
            for row in rows
        )

    def latest_snapshot(self, conn: sqlite3.Connection | None = None) -> PersistedSnapshot | None:
        target = conn if conn is not None else self._conn
        row = target.execute(
            "SELECT snapshot_id, version, cash, buying_power, non_marginable_buying_power, "
            "multiplier, as_of, reconciliation_status, captured_at, diagnostics "
            "FROM broker_snapshots ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return self._snapshot_from_row(row, target)

    def snapshot_by_version(
        self, version: int, conn: sqlite3.Connection | None = None
    ) -> PersistedSnapshot | None:
        target = conn if conn is not None else self._conn
        row = target.execute(
            "SELECT snapshot_id, version, cash, buying_power, non_marginable_buying_power, "
            "multiplier, as_of, reconciliation_status, captured_at, diagnostics "
            "FROM broker_snapshots WHERE version = ?",
            (version,),
        ).fetchone()
        if row is None:
            return None
        return self._snapshot_from_row(row, target)

    def _snapshot_from_row(self, row: sqlite3.Row, conn: sqlite3.Connection) -> PersistedSnapshot:
        snapshot_id = int(row["snapshot_id"])
        positions = tuple(
            Position(
                symbol=str(p["symbol"]),
                quantity=load_decimal(str(p["quantity"])),
                quantity_available=load_decimal(str(p["quantity_available"])),
                market_value=load_decimal(str(p["market_value"])),
            )
            for p in conn.execute(
                "SELECT symbol, quantity, quantity_available, market_value "
                "FROM position_snapshots WHERE snapshot_id = ? ORDER BY symbol",
                (snapshot_id,),
            )
        )
        assets = tuple(
            AssetState(
                symbol=str(a["symbol"]),
                status=AssetStatus(str(a["status"])),
                tradable=bool(a["tradable"]),
                fractionable=bool(a["fractionable"]),
            )
            for a in conn.execute(
                "SELECT symbol, status, tradable, fractionable "
                "FROM asset_snapshots WHERE snapshot_id = ? ORDER BY symbol",
                (snapshot_id,),
            )
        )
        orders = tuple(
            OrderSnapshotRecord(
                client_order_id=str(o["client_order_id"]),
                broker_order_id=None if o["broker_order_id"] is None else str(o["broker_order_id"]),
                symbol=str(o["symbol"]),
                side=str(o["side"]),
                alpaca_status=str(o["alpaca_status"]),
                mapped_state=str(o["mapped_state"]),
                quantity=None if o["quantity"] is None else load_decimal(str(o["quantity"])),
                filled_quantity=(
                    None
                    if o["filled_quantity"] is None
                    else load_decimal(str(o["filled_quantity"]))
                ),
            )
            for o in conn.execute(
                "SELECT client_order_id, broker_order_id, symbol, side, alpaca_status, "
                "mapped_state, quantity, filled_quantity "
                "FROM order_snapshots WHERE snapshot_id = ? ORDER BY client_order_id",
                (snapshot_id,),
            )
        )
        return PersistedSnapshot(
            snapshot_id=snapshot_id,
            version=int(row["version"]),
            broker=BrokerCashState(
                cash=load_decimal(str(row["cash"])),
                buying_power=load_decimal(str(row["buying_power"])),
                non_marginable_buying_power=load_decimal(str(row["non_marginable_buying_power"])),
                multiplier=load_decimal(str(row["multiplier"])),
                as_of=load_datetime(str(row["as_of"])),
            ),
            positions=positions,
            assets=assets,
            orders=orders,
            reconciliation_status=ReconciliationStatus(str(row["reconciliation_status"])),
            captured_at=load_datetime(str(row["captured_at"])),
            diagnostics=str(row["diagnostics"]),
        )

    def persist_snapshot(
        self,
        *,
        broker: BrokerCashState,
        positions: Sequence[Position],
        assets: Sequence[AssetState],
        orders: Sequence[OrderSnapshotRecord],
        status: ReconciliationStatus,
        captured_at: datetime,
        diagnostics: str,
        reasons: Sequence[str],
        conn: sqlite3.Connection,
    ) -> PersistedSnapshot:
        row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM broker_snapshots").fetchone()
        version = int(row["v"]) + 1
        cursor = conn.execute(
            "INSERT INTO broker_snapshots("
            "version, cash, buying_power, non_marginable_buying_power, multiplier, "
            "as_of, reconciliation_status, captured_at, diagnostics"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version,
                dump_decimal(broker.cash),
                dump_decimal(broker.buying_power),
                dump_decimal(broker.non_marginable_buying_power),
                dump_decimal(broker.multiplier),
                dump_datetime(broker.as_of),
                status.value,
                dump_datetime(captured_at),
                diagnostics,
            ),
        )
        rowid = cursor.lastrowid
        if rowid is None:
            raise PersistenceError("snapshot insert produced no rowid")
        snapshot_id = int(rowid)
        for position in positions:
            conn.execute(
                "INSERT INTO position_snapshots("
                "snapshot_id, symbol, quantity, quantity_available, market_value"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    position.symbol,
                    dump_decimal(position.quantity),
                    dump_decimal(position.quantity_available),
                    dump_decimal(position.market_value),
                ),
            )
        for asset in assets:
            conn.execute(
                "INSERT INTO asset_snapshots("
                "snapshot_id, symbol, status, tradable, fractionable"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    asset.symbol,
                    asset.status.value,
                    1 if asset.tradable else 0,
                    1 if asset.fractionable else 0,
                ),
            )
        for order in orders:
            conn.execute(
                "INSERT INTO order_snapshots("
                "snapshot_id, client_order_id, broker_order_id, symbol, side, "
                "alpaca_status, mapped_state, quantity, filled_quantity"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    order.client_order_id,
                    order.broker_order_id,
                    order.symbol,
                    order.side,
                    order.alpaca_status,
                    order.mapped_state,
                    None if order.quantity is None else dump_decimal(order.quantity),
                    None if order.filled_quantity is None else dump_decimal(order.filled_quantity),
                ),
            )
        conn.execute(
            "INSERT INTO reconciliations(snapshot_id, status, reasons, completed_at) "
            "VALUES (?, ?, ?, ?)",
            (
                snapshot_id,
                status.value,
                json.dumps(list(reasons), separators=(",", ":")),
                dump_datetime(captured_at),
            ),
        )
        loaded = self.snapshot_by_version(version, conn=conn)
        if loaded is None:
            raise PersistenceError("snapshot disappeared after insert")
        return loaded

    def active_reservations(
        self, conn: sqlite3.Connection | None = None
    ) -> tuple[ReservationRecord, ...]:
        target = conn if conn is not None else self._conn
        rows = target.execute(
            "SELECT reservation_id, proposal_id, kind, symbol, quantity, amount, "
            "client_order_id, leg_index, status, created_at "
            "FROM reservations WHERE status = ? ORDER BY reservation_id",
            (ReservationStatus.ACTIVE.value,),
        ).fetchall()
        return tuple(self._reservation_from_row(row) for row in rows)

    def _reservation_from_row(self, row: sqlite3.Row) -> ReservationRecord:
        return ReservationRecord(
            reservation_id=int(row["reservation_id"]),
            proposal_id=str(row["proposal_id"]),
            kind=ReservationKind(str(row["kind"])),
            symbol=None if row["symbol"] is None else str(row["symbol"]),
            quantity=None if row["quantity"] is None else load_decimal(str(row["quantity"])),
            amount=None if row["amount"] is None else load_decimal(str(row["amount"])),
            client_order_id=(
                None if row["client_order_id"] is None else str(row["client_order_id"])
            ),
            leg_index=None if row["leg_index"] is None else int(row["leg_index"]),
            status=ReservationStatus(str(row["status"])),
            created_at=load_datetime(str(row["created_at"])),
        )

    def get_proposal(
        self, proposal_id: str, conn: sqlite3.Connection | None = None
    ) -> ProposalRecord | None:
        target = conn if conn is not None else self._conn
        row = target.execute(
            "SELECT proposal_id, proposal_hash, status, authority_result, snapshot_version, "
            "created_at, expires_at, source_snapshot_id FROM proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            return None
        return ProposalRecord(
            proposal_id=str(row["proposal_id"]),
            proposal_hash=str(row["proposal_hash"]),
            status=ProposalRecordStatus(str(row["status"])),
            authority_result=AuthorityResult(str(row["authority_result"])),
            snapshot_version=int(row["snapshot_version"]),
            created_at=load_datetime(str(row["created_at"])),
            expires_at=None if row["expires_at"] is None else load_datetime(str(row["expires_at"])),
            source_snapshot_id=(
                None if row["source_snapshot_id"] is None else int(row["source_snapshot_id"])
            ),
        )

    def count_reservations(self, proposal_id: str, conn: sqlite3.Connection | None = None) -> int:
        target = conn if conn is not None else self._conn
        row = target.execute(
            "SELECT COUNT(*) AS n FROM reservations WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        return int(row["n"])

    def persist_proposal_decision(
        self,
        *,
        proposal: Proposal,
        proposal_hash: str,
        status: ProposalRecordStatus,
        decision: AuthorityDecision,
        snapshot: PersistedSnapshot,
        now: datetime,
        expires_at: datetime | None,
        conn: sqlite3.Connection,
    ) -> None:
        conn.execute(
            "INSERT INTO proposals("
            "proposal_id, proposal_hash, status, authority_result, snapshot_version, "
            "created_at, expires_at, source_snapshot_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                proposal.proposal_id,
                proposal_hash,
                status.value,
                decision.result.value,
                snapshot.version,
                dump_datetime(now),
                None if expires_at is None else dump_datetime(expires_at),
                snapshot.snapshot_id,
            ),
        )
        for leg in proposal.legs:
            conn.execute(
                "INSERT INTO proposal_legs("
                "proposal_id, leg_index, symbol, side, quantity, reference_price, "
                "client_order_id, notional"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    proposal.proposal_id,
                    leg.leg_index,
                    leg.symbol,
                    leg.side.value,
                    dump_decimal(leg.quantity),
                    dump_decimal(leg.reference_price),
                    leg.client_order_id,
                    dump_decimal(leg.notional),
                ),
            )
            conn.execute(
                "INSERT INTO order_identity(client_order_id, proposal_id, leg_index, created_at) "
                "VALUES (?, ?, ?, ?)",
                (leg.client_order_id, proposal.proposal_id, leg.leg_index, dump_datetime(now)),
            )
        for check in decision.policy_decision.results:
            conn.execute(
                "INSERT INTO policy_checks(proposal_id, check_id, passed, hard, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    proposal.proposal_id,
                    check.check_id.value,
                    1 if check.passed else 0,
                    1 if check.hard else 0,
                    check.detail,
                ),
            )
        conn.execute(
            "INSERT INTO authority_decisions(proposal_id, result, reasons, decided_at) "
            "VALUES (?, ?, ?, ?)",
            (
                proposal.proposal_id,
                decision.result.value,
                json.dumps(list(decision.reasons), separators=(",", ":")),
                dump_datetime(now),
            ),
        )
        if status is ProposalRecordStatus.APPROVAL_REQUIRED:
            if expires_at is None:
                raise PersistenceError("APPROVAL_REQUIRED requires expires_at")
            conn.execute(
                "INSERT INTO approvals("
                "proposal_id, payload_hash, authority_result, created_at, expires_at, "
                "snapshot_version"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    proposal.proposal_id,
                    proposal_hash,
                    decision.result.value,
                    dump_datetime(now),
                    dump_datetime(expires_at),
                    snapshot.version,
                ),
            )

    def persist_reservations(
        self,
        *,
        proposal: Proposal,
        now: datetime,
        conn: sqlite3.Connection,
    ) -> None:
        sell_qty: dict[str, Decimal] = {}
        sell_leg: dict[str, ProposedOrder] = {}
        for leg in proposal.sell_legs:
            sell_qty[leg.symbol] = sell_qty.get(leg.symbol, Decimal("0")) + leg.quantity
            sell_leg[leg.symbol] = leg
        for symbol, quantity in sell_qty.items():
            leg = sell_leg[symbol]
            conn.execute(
                "INSERT INTO reservations("
                "proposal_id, kind, symbol, quantity, amount, client_order_id, "
                "leg_index, status, created_at"
                ") VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (
                    proposal.proposal_id,
                    ReservationKind.SELL_QUANTITY.value,
                    symbol,
                    dump_decimal(quantity),
                    leg.client_order_id,
                    leg.leg_index,
                    ReservationStatus.ACTIVE.value,
                    dump_datetime(now),
                ),
            )
        buy_notional = proposal.total_buy_notional
        if buy_notional > 0:
            conn.execute(
                "INSERT INTO reservations("
                "proposal_id, kind, symbol, quantity, amount, client_order_id, "
                "leg_index, status, created_at"
                ") VALUES (?, ?, NULL, NULL, ?, NULL, NULL, ?, ?)",
                (
                    proposal.proposal_id,
                    ReservationKind.CASH_DEPLOYMENT.value,
                    dump_decimal(buy_notional),
                    ReservationStatus.ACTIVE.value,
                    dump_datetime(now),
                ),
            )
        for leg in proposal.legs:
            conn.execute(
                "INSERT INTO reservations("
                "proposal_id, kind, symbol, quantity, amount, client_order_id, "
                "leg_index, status, created_at"
                ") VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?)",
                (
                    proposal.proposal_id,
                    ReservationKind.ORDER_IDENTITY.value,
                    leg.symbol,
                    leg.client_order_id,
                    leg.leg_index,
                    ReservationStatus.ACTIVE.value,
                    dump_datetime(now),
                ),
            )
        notional = proposal.total_buy_notional + proposal.total_sell_notional
        conn.execute(
            "INSERT INTO autonomous_executions(proposal_id, timestamp, notional) VALUES (?, ?, ?)",
            (proposal.proposal_id, dump_datetime(now), dump_decimal(notional)),
        )

    def load_autonomous_history(
        self, conn: sqlite3.Connection | None = None
    ) -> tuple[AutonomousExecution, ...]:
        target = conn if conn is not None else self._conn
        rows = target.execute(
            "SELECT timestamp, notional FROM autonomous_executions ORDER BY id"
        ).fetchall()
        return tuple(
            AutonomousExecution(
                timestamp=load_datetime(str(row["timestamp"])),
                notional=load_decimal(str(row["notional"])),
            )
            for row in rows
        )

    def load_unknown_orders(
        self, conn: sqlite3.Connection | None = None
    ) -> tuple[UnknownOrderRecord, ...]:
        target = conn if conn is not None else self._conn
        rows = target.execute(
            "SELECT client_order_id, proposal_id, symbol, side, quantity, filled_quantity, "
            "state, last_lookup_at, created_at FROM unknown_orders ORDER BY client_order_id"
        ).fetchall()
        return tuple(
            UnknownOrderRecord(
                client_order_id=str(row["client_order_id"]),
                proposal_id=str(row["proposal_id"]),
                symbol=str(row["symbol"]),
                side=str(row["side"]),
                quantity=None if row["quantity"] is None else load_decimal(str(row["quantity"])),
                filled_quantity=(
                    None
                    if row["filled_quantity"] is None
                    else load_decimal(str(row["filled_quantity"]))
                ),
                state=str(row["state"]),
                last_lookup_at=(
                    None
                    if row["last_lookup_at"] is None
                    else load_datetime(str(row["last_lookup_at"]))
                ),
                created_at=load_datetime(str(row["created_at"])),
            )
            for row in rows
        )

    def upsert_unknown_order(self, record: UnknownOrderRecord, conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO unknown_orders("
            "client_order_id, proposal_id, symbol, side, quantity, filled_quantity, "
            "state, last_lookup_at, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(client_order_id) DO UPDATE SET "
            "state = excluded.state, last_lookup_at = excluded.last_lookup_at, "
            "quantity = excluded.quantity, filled_quantity = excluded.filled_quantity",
            (
                record.client_order_id,
                record.proposal_id,
                record.symbol,
                record.side,
                None if record.quantity is None else dump_decimal(record.quantity),
                None if record.filled_quantity is None else dump_decimal(record.filled_quantity),
                record.state,
                None if record.last_lookup_at is None else dump_datetime(record.last_lookup_at),
                dump_datetime(record.created_at),
            ),
        )

    def record_audit(
        self,
        event_type: AuditEventType,
        timestamp: datetime,
        *,
        reason: str,
        proposal_id: str | None = None,
        snapshot_version: int | None = None,
        detail: str = "",
        broker_identifiers: str = "",
        conn: sqlite3.Connection | None = None,
    ) -> None:
        target = conn if conn is not None else self._conn
        target.execute(
            "INSERT INTO audit_events("
            "event_type, timestamp, proposal_id, snapshot_version, reason, detail, "
            "broker_identifiers"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event_type.value,
                dump_datetime(timestamp),
                proposal_id,
                snapshot_version,
                reason,
                detail,
                broker_identifiers,
            ),
        )

    def list_audit(
        self,
        *,
        event_type: AuditEventType | None = None,
        proposal_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[AuditEvent, ...]:
        target = conn if conn is not None else self._conn
        sql = (
            "SELECT event_type, timestamp, proposal_id, snapshot_version, reason, detail, "
            "broker_identifiers FROM audit_events WHERE 1=1"
        )
        params: list[object] = []
        if event_type is not None:
            sql += " AND event_type = ?"
            params.append(event_type.value)
        if proposal_id is not None:
            sql += " AND proposal_id = ?"
            params.append(proposal_id)
        sql += " ORDER BY id"
        rows = target.execute(sql, params).fetchall()
        return tuple(
            AuditEvent(
                event_type=AuditEventType(str(row["event_type"])),
                timestamp=load_datetime(str(row["timestamp"])),
                proposal_id=None if row["proposal_id"] is None else str(row["proposal_id"]),
                snapshot_version=(
                    None if row["snapshot_version"] is None else int(row["snapshot_version"])
                ),
                reason=str(row["reason"]),
                detail=str(row["detail"]),
                broker_identifiers=str(row["broker_identifiers"]),
            )
            for row in rows
        )

    def load_ledger(self, conn: sqlite3.Connection | None = None) -> LocalLedger:
        target = conn if conn is not None else self._conn
        snapshot = self.latest_snapshot(conn=target)
        as_of = snapshot.broker.as_of.date() if snapshot is not None else date.min
        return LocalLedger(
            scenario=self.get_scenario(conn=target),
            snapshot=snapshot,
            reservations=self.active_reservations(conn=target),
            unknown_orders=self.load_unknown_orders(conn=target),
            settlement_as_of=as_of,
        )
