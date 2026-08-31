"""Read-only live-paper preflight. Never submits, cancels, or mutates the broker.

A passing preflight is observational. It is not execution authority. The
mutating smoke requires a separate human opt-in and must re-run fresh quote,
reconciliation, TreasuryGuard, authority, kill switch, and LIMIT validation
immediately before submit. No stale preflight result may authorize a trade.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from opaca.broker.adapters import parse_decimal_field
from opaca.broker.errors import PaperEnvironmentError
from opaca.broker.gateway import (
    ASSET_UNIVERSE,
    LIVE_ENDPOINT,
    AlpacaGateway,
    assert_read_only_gateway,
    require_paper_endpoint,
)
from opaca.broker.paper import ENV_KEY_ID, ENV_SECRET
from opaca.calendar.us_trading_calendar import US_TRADING_CALENDAR
from opaca.domain.models import AuthorityResult
from opaca.domain.money import ZERO
from opaca.market.binding import bind_buy, bind_single_leg_proposal
from opaca.market.errors import MarketDataError
from opaca.market.limit import DEFAULT_BUY_LIMIT_TOLERANCE
from opaca.market.quote import (
    DEFAULT_MAX_QUOTE_AGE_SECONDS,
    CanonicalMarketPrice,
    quote_age_seconds,
    validate_canonical_quote,
)
from opaca.market.source import ReadOnlyMarketData, latest_trades
from opaca.orchestration.context import build_policy_context
from opaca.persistence.demo import PAPER_DEMO_DB_NAME, init_paper_demo_store
from opaca.persistence.schema import SCHEMA_VERSION
from opaca.persistence.store import SQLiteStore
from opaca.persistence.types import ReconciliationStatus
from opaca.policy.decision import decide
from opaca.reconciliation.service import reconcile

PREFLIGHT_PROPOSAL_ID = "preflight-sgov-buy-1"
EXECUTION_NOT_ATTEMPTED = "NOT ATTEMPTED"


@dataclass(frozen=True)
class PreflightReport:
    paper_account: str
    cash: Decimal | None
    quote_symbol: str | None
    quote_price: Decimal | None
    quote_timestamp: datetime | None
    quote_age_seconds: float | None
    quote_source: str | None
    limit_price: Decimal | None
    max_buy_notional: Decimal | None
    treasuryguard: str
    authority: str
    db_schema: str
    db_fresh: bool
    db_path: str
    execution: str
    fail_reason: str | None
    ran: bool

    def render(self) -> str:
        cash_text = "n/a" if self.cash is None else format(self.cash, "f")
        quote_ts = "n/a" if self.quote_timestamp is None else self.quote_timestamp.isoformat()
        age = "n/a" if self.quote_age_seconds is None else str(self.quote_age_seconds)
        limit_text = "n/a" if self.limit_price is None else format(self.limit_price, "f")
        max_text = "n/a" if self.max_buy_notional is None else format(self.max_buy_notional, "f")
        lines = [
            "PAPER ACCOUNT:",
            self.paper_account,
            "",
            "CASH:",
            cash_text,
            "",
            "QUOTE:",
            f"symbol {self.quote_symbol or 'n/a'}",
            f"price {self.quote_price if self.quote_price is not None else 'n/a'}",
            f"timestamp {quote_ts}",
            f"age {age}",
            f"source {self.quote_source or 'n/a'}",
            "",
            "LIMIT:",
            limit_text,
            "",
            "MAX BUY NOTIONAL:",
            max_text,
            "",
            "TREASURYGUARD:",
            self.treasuryguard,
            "",
            "AUTHORITY:",
            self.authority,
            "",
            "DB:",
            self.db_schema,
            "fresh" if self.db_fresh else "existing-refused-or-unusable",
            self.db_path,
            "",
            "EXECUTION:",
            self.execution,
        ]
        if self.fail_reason:
            lines.extend(["", "FAIL REASON:", self.fail_reason])
        return "\n".join(lines) + "\n"


def credentials_present() -> bool:
    return bool(os.environ.get(ENV_KEY_ID, "").strip() and os.environ.get(ENV_SECRET, "").strip())


def _verify_paper_endpoint(gateway: object) -> str:
    endpoint = str(getattr(gateway, "endpoint", "") or "")
    if endpoint == LIVE_ENDPOINT:
        raise PaperEnvironmentError("live Alpaca endpoint is forbidden")
    return require_paper_endpoint(endpoint)


def _account_status(account: Mapping[str, object]) -> tuple[str, Decimal | None]:
    status = str(account.get("status", "")).upper()
    paper_account = "ACTIVE" if status == "ACTIVE" else "FAIL"
    cash: Decimal | None
    try:
        cash = parse_decimal_field(account, "cash")
    except Exception:
        cash = None
        paper_account = "FAIL"
    return paper_account, cash


def _fail(
    *,
    paper_account: str,
    cash: Decimal | None,
    db_path: str,
    db_fresh: bool,
    reason: str,
    quote: CanonicalMarketPrice | None = None,
    quote_age: float | None = None,
    limit_price: Decimal | None = None,
    max_buy_notional: Decimal | None = None,
    treasuryguard: str = "REJECT",
    authority: str = "REJECT",
) -> PreflightReport:
    return PreflightReport(
        paper_account=paper_account,
        cash=cash,
        quote_symbol=None if quote is None else quote.symbol,
        quote_price=None if quote is None else quote.price,
        quote_timestamp=None if quote is None else quote.source_timestamp,
        quote_age_seconds=quote_age,
        quote_source=None if quote is None else quote.source,
        limit_price=limit_price,
        max_buy_notional=max_buy_notional,
        treasuryguard=treasuryguard,
        authority=authority,
        db_schema=f"schema v{SCHEMA_VERSION}",
        db_fresh=db_fresh,
        db_path=db_path,
        execution=EXECUTION_NOT_ATTEMPTED,
        fail_reason=reason,
        ran=True,
    )


def run_read_only_preflight(
    read_gateway: AlpacaGateway,
    market_data: ReadOnlyMarketData,
    *,
    now: datetime,
    db_path: str | Path,
    overwrite_db: bool = False,
    max_quote_age_seconds: int = DEFAULT_MAX_QUOTE_AGE_SECONDS,
    buy_limit_tolerance: Decimal = DEFAULT_BUY_LIMIT_TOLERANCE,
) -> PreflightReport:
    """Read-only preflight. Constructs a 1-share SGOV BUY proposal and stops.

    Does not call submit or cancel. Does not reserve execution capacity.
    Does not import or construct a mutating gateway.
    """
    assert_read_only_gateway(read_gateway)
    db_path_text = str(db_path)
    try:
        _verify_paper_endpoint(read_gateway)
        account = read_gateway.get_account()
        paper_account, cash = _account_status(account)
        _ = read_gateway.get_positions()
        for symbol in ASSET_UNIVERSE:
            read_gateway.get_asset(symbol)
        clock = read_gateway.get_clock()
        _ = clock
        calendar_end = now.date() + timedelta(days=7)
        _ = read_gateway.get_calendar(now.date(), calendar_end)
    except Exception as exc:
        return _fail(
            paper_account="FAIL",
            cash=None,
            db_path=db_path_text,
            db_fresh=False,
            reason=str(exc),
        )

    if paper_account != "ACTIVE":
        return _fail(
            paper_account=paper_account,
            cash=cash,
            db_path=db_path_text,
            db_fresh=False,
            reason="paper account is not ACTIVE",
        )

    quote: CanonicalMarketPrice | None = None
    quotes: dict[str, CanonicalMarketPrice]
    try:
        quotes = latest_trades(market_data, ASSET_UNIVERSE)
        quote = quotes["SGOV"]
        validate_canonical_quote(quote, now=now, max_age_seconds=max_quote_age_seconds)
        for symbol, item in quotes.items():
            if symbol == "SGOV":
                continue
            validate_canonical_quote(item, now=now, max_age_seconds=max_quote_age_seconds)
    except MarketDataError as exc:
        age = None
        if quote is not None:
            try:
                age = quote_age_seconds(quote, now=now)
            except Exception:
                age = None
        return _fail(
            paper_account=paper_account,
            cash=cash,
            db_path=db_path_text,
            db_fresh=False,
            reason=str(exc),
            quote=quote,
            quote_age=age,
        )

    bound = bind_buy(quote, Decimal("1"), tolerance=buy_limit_tolerance)
    age = quote_age_seconds(quote, now=now)
    store: SQLiteStore | None = None
    db_fresh = not Path(db_path).exists()
    try:
        store = init_paper_demo_store(
            db_path,
            now=now,
            overwrite=overwrite_db,
            opening_cash=cash if cash is not None else ZERO,
        )
        db_fresh = True
        recon = reconcile(store, read_gateway, now=now)
        if recon.status is not ReconciliationStatus.RECONCILED or recon.snapshot is None:
            return _fail(
                paper_account=paper_account,
                cash=cash,
                db_path=store.path,
                db_fresh=db_fresh,
                reason=f"reconciliation {recon.status.value}",
                quote=quote,
                quote_age=age,
                limit_price=bound.limit_price,
                max_buy_notional=bound.max_cash_obligation,
            )
        proposal, prices, bindings = bind_single_leg_proposal(PREFLIGHT_PROPOSAL_ID, bound, quotes)
        _ = bindings
        context, _snapshot = build_policy_context(
            store,
            now=now,
            prices=prices,
            calendar=US_TRADING_CALENDAR,
            environment_verified=True,
        )
        decision = decide(proposal, context)
        tg = "PASS" if decision.policy_decision.passed else "REJECT"
        if decision.result is AuthorityResult.AUTO:
            authority = "AUTO"
        elif decision.result is AuthorityResult.APPROVAL_REQUIRED:
            authority = "APPROVAL"
        else:
            authority = "REJECT"
        return PreflightReport(
            paper_account=paper_account,
            cash=cash,
            quote_symbol=quote.symbol,
            quote_price=quote.price,
            quote_timestamp=quote.source_timestamp,
            quote_age_seconds=age,
            quote_source=quote.source,
            limit_price=bound.limit_price,
            max_buy_notional=bound.max_cash_obligation,
            treasuryguard=tg,
            authority=authority,
            db_schema=f"schema v{store.schema_version()}",
            db_fresh=db_fresh,
            db_path=store.path,
            execution=EXECUTION_NOT_ATTEMPTED,
            fail_reason=None if tg == "PASS" else "; ".join(decision.reasons),
            ran=True,
        )
    except Exception as exc:
        return _fail(
            paper_account=paper_account,
            cash=cash,
            db_path=db_path_text,
            db_fresh=False,
            reason=str(exc),
            quote=quote,
            quote_age=age,
            limit_price=bound.limit_price,
            max_buy_notional=bound.max_cash_obligation,
        )
    finally:
        if store is not None:
            store.close()


def not_run_report(reason: str, *, db_path: str) -> PreflightReport:
    return PreflightReport(
        paper_account="FAIL",
        cash=None,
        quote_symbol=None,
        quote_price=None,
        quote_timestamp=None,
        quote_age_seconds=None,
        quote_source=None,
        limit_price=None,
        max_buy_notional=None,
        treasuryguard="REJECT",
        authority="REJECT",
        db_schema=f"schema v{SCHEMA_VERSION}",
        db_fresh=False,
        db_path=db_path,
        execution=EXECUTION_NOT_ATTEMPTED,
        fail_reason=reason,
        ran=False,
    )


def run_live_preflight_from_env(
    *,
    db_path: str | Path = PAPER_DEMO_DB_NAME,
    overwrite_db: bool = False,
    now: datetime | None = None,
) -> PreflightReport:
    if not credentials_present():
        return not_run_report("credentials absent", db_path=str(db_path))
    from opaca.broker.alpaca import open_paper_gateway_from_env
    from opaca.market.source import open_paper_market_data_from_env

    gateway = open_paper_gateway_from_env()
    market = open_paper_market_data_from_env()
    return run_read_only_preflight(
        gateway,
        market,
        now=datetime.now(UTC) if now is None else now,
        db_path=db_path,
        overwrite_db=overwrite_db,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="opaca preflight",
        description="Read-only PAPER preflight. Never submits an order.",
    )
    parser.add_argument("--db", default=PAPER_DEMO_DB_NAME, help="schema-v2 paper-demo DB path")
    parser.add_argument(
        "--overwrite-db",
        action="store_true",
        help="explicitly replace an existing demo DB (never the default)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = run_live_preflight_from_env(db_path=args.db, overwrite_db=args.overwrite_db)
    sys.stdout.write(report.render())
    if not report.ran:
        sys.stdout.write("READ-ONLY PREFLIGHT: NOT RUN\n")
        return 2
    if report.fail_reason or report.paper_account != "ACTIVE":
        return 1
    return 0
