#!/usr/bin/env python3
"""Opaca Phase −1 Broker Reality Spike.

Phase −1A subcommands are READ-ONLY:

    python spike/spike.py account
    python spike/spike.py assets
    python spike/spike.py clock
    python spike/spike.py calendar

Broker-mutating experiments (Phase −1B) are deliberately NOT present here
and must be added as separate, explicitly-invoked subcommands later.

Safety invariants:
  * Paper endpoint only: https://paper-api.alpaca.markets — no override is accepted.
  * Credentials come from the shell environment: APCA_API_KEY_ID / APCA_API_SECRET_KEY.
  * Secrets are never printed, logged, or serialized into evidence.
  * No order-submission endpoint is reachable from this module.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

PAPER_ENDPOINT = "https://paper-api.alpaca.markets"
EVIDENCE_DIR = Path(__file__).resolve().parent / "evidence"
ASSET_UNIVERSE = ("SGOV", "BIL", "SHV")
CALENDAR_DAYS_AHEAD = 45
SGT = dt.timezone(dt.timedelta(hours=8), "SGT")


def fail(message: str) -> None:
    print(f"BLOCKED: {message}", file=sys.stderr)
    sys.exit(2)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def require_credentials() -> tuple[str, str]:
    key_id = os.environ.get("APCA_API_KEY_ID", "").strip()
    secret_key = os.environ.get("APCA_API_SECRET_KEY", "").strip()
    missing = [
        name
        for name, value in (
            ("APCA_API_KEY_ID", key_id),
            ("APCA_API_SECRET_KEY", secret_key),
        )
        if not value
    ]
    if missing:
        fail(
            "paper credentials not found in environment: "
            + ", ".join(missing)
            + " unset. Supply Alpaca PAPER credentials via shell environment."
        )
    return key_id, secret_key


def make_paper_client():
    from alpaca.trading.client import TradingClient

    key_id, secret_key = require_credentials()
    client = TradingClient(api_key=key_id, secret_key=secret_key, paper=True)
    base_url = getattr(client, "_base_url", "") or ""
    base_url = str(getattr(base_url, "value", base_url)).rstrip("/")
    if not base_url.startswith(PAPER_ENDPOINT):
        fail(
            "paper endpoint not confirmed; expected prefix "
            f"{PAPER_ENDPOINT!r}, observed {base_url or 'unknown'!r}"
        )
    return client


def write_evidence(experiment: str, observations: dict) -> Path:
    EVIDENCE_DIR.mkdir(exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = EVIDENCE_DIR / f"{experiment}_{stamp}.json"
    record = {
        "experiment": experiment,
        "generated_at_utc": utcnow().isoformat(timespec="seconds"),
        "environment": {"endpoint": PAPER_ENDPOINT, "mode": "paper"},
        "observations": observations,
    }
    path.write_text(json.dumps(record, indent=2, default=str) + "\n")
    return path


def to_fields(obj) -> dict:
    dump = getattr(obj, "model_dump", None) or getattr(obj, "dict", None)
    return dump() if dump else dict(obj)


def parse_broker_time(value) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        try:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def with_sgt(value, parsed: dt.datetime | None) -> dict:
    entry = {"raw": str(value)}
    if parsed is not None:
        entry["utc"] = parsed.isoformat()
        entry["singapore"] = parsed.astimezone(SGT).isoformat()
    return entry


def cmd_account(_: argparse.Namespace) -> None:
    client = make_paper_client()
    account = client.get_account()
    fields = to_fields(account)

    redacted = ("id", "account_number")
    sanitized = {k: v for k, v in fields.items() if k not in redacted}

    def money(key: str) -> float | None:
        try:
            return float(fields.get(key))
        except (TypeError, ValueError):
            return None

    cash = money("cash")
    buying_power = money("buying_power")
    non_marginable = money("non_marginable_buying_power")
    comparison = {
        "cash": cash,
        "buying_power": buying_power,
        "non_marginable_buying_power": non_marginable,
        "buying_power_minus_cash": (
            None if buying_power is None or cash is None else round(buying_power - cash, 2)
        ),
        "cash_minus_non_marginable": (
            None if cash is None or non_marginable is None else round(cash - non_marginable, 2)
        ),
        "note": (
            "buying_power must NOT be treated as usable Opaca corporate cash; "
            "non_marginable_buying_power is the closest conservative proxy"
        ),
    }

    public_methods = [
        name
        for name in dir(client)
        if not name.startswith("_") and callable(getattr(client, name, None))
    ]
    reset_probe = sorted(
        name
        for name in public_methods
        if any(
            term in name.lower()
            for term in ("reset", "balance", "deposit", "transfer", "recipient")
        )
    )

    observations = {
        "account_fields": sanitized,
        "cash_vs_buying_power": comparison,
        "demo_baseline_probe": {
            "observed_cash": cash,
            "observed_equity": fields.get("equity"),
            "target_paper_balance": 500000,
            "reset_related_client_methods": reset_probe,
            "note": (
                "read-only probe of the Trading API surface only; no reset or "
                "balance-setting call was made"
            ),
        },
    }
    path = write_evidence("account", observations)
    print(f"OK account evidence -> {path}")
    print(json.dumps(comparison, indent=2))


def cmd_assets(_: argparse.Namespace) -> None:
    client = make_paper_client()
    results: dict[str, dict] = {}
    for symbol in ASSET_UNIVERSE:
        try:
            asset = client.get_asset(symbol)
            fields = to_fields(asset)
            fields.pop("id", None)
            results[symbol] = {
                "error": None,
                "fields": fields,
                "tradability": {
                    "status": fields.get("status"),
                    "tradable": fields.get("tradable"),
                    "fractionable": fields.get("fractionable"),
                    "marginable": fields.get("marginable"),
                    "shortable": fields.get("shortable"),
                    "easy_to_borrow": fields.get("easy_to_borrow"),
                    "exchange": fields.get("exchange"),
                    "asset_class": fields.get("class"),
                },
            }
        except Exception as exc:  # noqa: BLE001 - evidence capture, sanitized below
            results[symbol] = {"error": type(exc).__name__, "fields": None, "tradability": None}
    path = write_evidence("assets", {"universe": list(ASSET_UNIVERSE), "assets": results})
    print(f"OK assets evidence -> {path}")
    for symbol, entry in results.items():
        tradability = entry["tradability"] or {}
        print(
            f"{symbol}: error={entry['error']} status={tradability.get('status')} "
            f"tradable={tradability.get('tradable')} fractionable={tradability.get('fractionable')}"
        )


def cmd_clock(_: argparse.Namespace) -> None:
    client = make_paper_client()
    clock = client.get_clock()
    fields = to_fields(clock)
    observations = {
        "clock_fields": fields,
        "broker_times": {
            "timestamp": with_sgt(fields.get("timestamp"), parse_broker_time(fields.get("timestamp"))),
            "next_open": with_sgt(fields.get("next_open"), parse_broker_time(fields.get("next_open"))),
            "next_close": with_sgt(fields.get("next_close"), parse_broker_time(fields.get("next_close"))),
        },
        "is_open": fields.get("is_open"),
        "note": "raw broker timestamps preserved; SGT shown for human reading only",
    }
    path = write_evidence("clock", observations)
    print(f"OK clock evidence -> {path}")
    print(json.dumps(observations["broker_times"], indent=2))


def cmd_calendar(_: argparse.Namespace) -> None:
    from alpaca.trading.requests import GetCalendarRequest

    client = make_paper_client()
    clock = client.get_clock()
    base = parse_broker_time(to_fields(clock).get("timestamp")) or utcnow()
    start = base.date()
    end = start + dt.timedelta(days=CALENDAR_DAYS_AHEAD)
    sessions = client.get_calendar(filters=GetCalendarRequest(start=start, end=end))
    rows = [to_fields(session) for session in sessions]
    dates = []
    for row in rows:
        parsed = parse_broker_time(row.get("date"))
        if parsed is not None:
            dates.append(parsed.date())
    date_set = set(dates)
    missing_weekdays, early_closes = [], []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5 and cursor not in date_set:
            missing_weekdays.append(cursor.isoformat())
        cursor += dt.timedelta(days=1)
    for row in rows:
        close_value = str(row.get("close", ""))
        close_time = parse_broker_time(close_value)
        close_hhmm = close_time.strftime("%H:%M") if close_time else close_value
        if close_value and close_hhmm != "16:00":
            early_closes.append(
                {"date": str(row.get("date")), "close": close_value, "close_time": close_hhmm}
            )
    observations = {
        "requested_window": {"start": start.isoformat(), "end": end.isoformat()},
        "session_count": len(rows),
        "sessions": rows,
        "derived": {
            "missing_weekdays_within_window": missing_weekdays,
            "early_close_sessions": early_closes,
            "weekend_dates_absent": all(d.weekday() < 5 for d in dates),
            "note": (
                "derived observations only; production T+1 settlement logic is "
                "intentionally NOT implemented in the spike"
            ),
        },
    }
    path = write_evidence("calendar", observations)
    print(f"OK calendar evidence -> {path}")
    print(
        f"sessions={len(rows)} missing_weekdays={len(missing_weekdays)} "
        f"early_closes={len(early_closes)}"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Opaca Phase −1 broker reality spike (read-only experiments only)"
    )
    sub = parser.add_subparsers(dest="experiment", required=True)
    sub.add_parser("account", help="A1: read-only account reality").set_defaults(fn=cmd_account)
    sub.add_parser("assets", help="A2: read-only asset tradability").set_defaults(fn=cmd_assets)
    sub.add_parser("clock", help="A3: read-only market clock").set_defaults(fn=cmd_clock)
    sub.add_parser("calendar", help="A4: read-only trading calendar").set_defaults(fn=cmd_calendar)
    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
