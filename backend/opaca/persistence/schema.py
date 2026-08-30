"""Deterministic SQLite schema bootstrap (WAL, foreign_keys, version 1)."""

from __future__ import annotations

SCHEMA_VERSION = 1

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE policies (
        name TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        value_type TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE obligations (
        obligation_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        amount TEXT NOT NULL,
        due_date TEXT NOT NULL,
        seeded INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE scenario_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        opening_cash TEXT NOT NULL,
        seeded_at TEXT NOT NULL,
        operating_reserve TEXT NOT NULL,
        investable_surplus TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE broker_snapshots (
        snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
        version INTEGER NOT NULL UNIQUE,
        cash TEXT NOT NULL,
        buying_power TEXT NOT NULL,
        non_marginable_buying_power TEXT NOT NULL,
        multiplier TEXT NOT NULL,
        as_of TEXT NOT NULL,
        reconciliation_status TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        diagnostics TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE position_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_id INTEGER NOT NULL REFERENCES broker_snapshots(snapshot_id),
        symbol TEXT NOT NULL,
        quantity TEXT NOT NULL,
        quantity_available TEXT NOT NULL,
        market_value TEXT NOT NULL,
        UNIQUE(snapshot_id, symbol)
    )
    """,
    """
    CREATE TABLE asset_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_id INTEGER NOT NULL REFERENCES broker_snapshots(snapshot_id),
        symbol TEXT NOT NULL,
        status TEXT NOT NULL,
        tradable INTEGER NOT NULL,
        fractionable INTEGER NOT NULL,
        UNIQUE(snapshot_id, symbol)
    )
    """,
    """
    CREATE TABLE order_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_id INTEGER NOT NULL REFERENCES broker_snapshots(snapshot_id),
        client_order_id TEXT NOT NULL,
        broker_order_id TEXT,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        alpaca_status TEXT NOT NULL,
        mapped_state TEXT NOT NULL,
        quantity TEXT,
        filled_quantity TEXT,
        UNIQUE(snapshot_id, client_order_id)
    )
    """,
    """
    CREATE TABLE settlement_events (
        event_id TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        settlement_date TEXT NOT NULL,
        amount TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE proposals (
        proposal_id TEXT PRIMARY KEY,
        proposal_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        authority_result TEXT NOT NULL,
        snapshot_version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT,
        source_snapshot_id INTEGER REFERENCES broker_snapshots(snapshot_id)
    )
    """,
    """
    CREATE TABLE proposal_legs (
        proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
        leg_index INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        quantity TEXT NOT NULL,
        reference_price TEXT NOT NULL,
        client_order_id TEXT NOT NULL,
        notional TEXT NOT NULL,
        PRIMARY KEY (proposal_id, leg_index),
        UNIQUE(client_order_id)
    )
    """,
    """
    CREATE TABLE policy_checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
        check_id TEXT NOT NULL,
        passed INTEGER NOT NULL,
        hard INTEGER NOT NULL,
        detail TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE authority_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
        result TEXT NOT NULL,
        reasons TEXT NOT NULL,
        decided_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE approvals (
        proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id),
        payload_hash TEXT NOT NULL,
        authority_result TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        snapshot_version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE reservations (
        reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
        kind TEXT NOT NULL,
        symbol TEXT,
        quantity TEXT,
        amount TEXT,
        client_order_id TEXT,
        leg_index INTEGER,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE order_identity (
        client_order_id TEXT PRIMARY KEY,
        proposal_id TEXT NOT NULL,
        leg_index INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(proposal_id, leg_index)
    )
    """,
    """
    CREATE TABLE autonomous_executions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        notional TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE unknown_orders (
        client_order_id TEXT PRIMARY KEY,
        proposal_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        quantity TEXT,
        filled_quantity TEXT,
        state TEXT NOT NULL,
        last_lookup_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        proposal_id TEXT,
        snapshot_version INTEGER,
        reason TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        broker_identifiers TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE reconciliations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_id INTEGER NOT NULL REFERENCES broker_snapshots(snapshot_id),
        status TEXT NOT NULL,
        reasons TEXT NOT NULL,
        completed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE system_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX idx_reservations_active_sell
        ON reservations(proposal_id, symbol)
        WHERE kind = 'SELL_QUANTITY' AND status = 'ACTIVE'
    """,
    """
    CREATE UNIQUE INDEX idx_reservations_active_cash
        ON reservations(proposal_id)
        WHERE kind = 'CASH_DEPLOYMENT' AND status = 'ACTIVE'
    """,
    """
    CREATE INDEX idx_reservations_proposal ON reservations(proposal_id)
    """,
    """
    CREATE INDEX idx_audit_proposal ON audit_events(proposal_id)
    """,
    """
    CREATE INDEX idx_audit_type ON audit_events(event_type)
    """,
)

#: Named policy defaults (SPEC s15). Amounts match the treasury-core test
#: rehearsal values so AUTO remains reachable for routine demo sizes.
DEFAULT_POLICY_ROWS: tuple[tuple[str, str, str], ...] = (
    ("concentration_max_fraction", "0.70", "decimal"),
    ("per_order_autonomous_notional_max", "25000", "decimal"),
    ("per_proposal_aggregate_notional_max", "25000", "decimal"),
    ("rolling_24h_autonomous_notional_max", "50000", "decimal"),
    ("rolling_autonomous_order_count_max", "10", "int"),
    ("runaway_hourly_order_count_max", "6", "int"),
    ("min_trade_notional", "1.00", "decimal"),
    ("preclose_blackout_enabled", "1", "bool"),
    ("preclose_blackout_minutes", "15", "int"),
    ("approval_expiry_seconds", "300", "int"),
    ("max_snapshot_age_seconds", "60", "int"),
    ("permitted_symbols", '["SGOV", "BIL", "SHV"]', "json"),
)

DEFAULT_MAX_SNAPSHOT_AGE_SECONDS = 60

DEFAULT_SYSTEM_STATE: tuple[tuple[str, str], ...] = (("kill_switch", "0"),)
