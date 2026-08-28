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
import hashlib
import json
import os
import sys
import time
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


SPIKE_PREFIX = "opaca-spike"
TERMINAL_STATUSES = {
    "filled",
    "canceled",
    "expired",
    "rejected",
    "done_for_day",
    "stopped",
    "suspended",
    "calculated",
}
ACCOUNT_REDACTIONS = ("id", "account_number")


def default_run_id() -> str:
    return utcnow().strftime("%Y%m%d") + "-r1"


def client_order_id(experiment: str, leg: str, run_id: str) -> str:
    material = f"{SPIKE_PREFIX}:{experiment}:{run_id}:{leg}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def probe_client_order_id(length: int, experiment: str, leg: str, run_id: str) -> str:
    material = f"{SPIKE_PREFIX}:{experiment}:{run_id}:{leg}:probe"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    while len(digest) < length:
        digest += hashlib.sha256(digest.encode("utf-8")).hexdigest()
    return digest[:length]


def get_state(client) -> dict:
    account = to_fields(client.get_account())
    positions = [to_fields(p) for p in client.get_all_positions()]
    from alpaca.trading.requests import GetOrdersRequest

    open_orders = [to_fields(o) for o in client.get_orders(GetOrdersRequest(status="open"))]
    return {
        "captured_at_utc": utcnow().isoformat(timespec="seconds"),
        "account": {k: v for k, v in account.items() if k not in ACCOUNT_REDACTIONS},
        "positions": positions,
        "open_orders": open_orders,
    }


def preflight_guard(client) -> dict:
    state = get_state(client)
    acct = state["account"]

    def dec(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    cash = dec(acct.get("cash"))
    buying_power = dec(acct.get("buying_power"))
    non_marginable = dec(acct.get("non_marginable_buying_power"))
    problems = []
    status_value = str(getattr(acct.get("status"), "value", acct.get("status")))
    if status_value != "ACTIVE":
        problems.append(f"account status is {status_value}, not ACTIVE")
    for flag in ("trading_blocked", "account_blocked", "transfers_blocked", "trade_suspended_by_user"):
        if acct.get(flag):
            problems.append(f"{flag} is set")
    if cash is None or cash < 0:
        problems.append("cash missing or negative")
    if None not in (buying_power, cash) and buying_power + 0.01 < cash:
        problems.append("buying_power below cash (material change vs Phase −1A)")
    if None not in (non_marginable, cash) and non_marginable + 0.01 < cash:
        problems.append("non_marginable_buying_power below cash (material change vs Phase −1A)")
    if problems:
        fail("preflight hard-stop: " + "; ".join(problems))
    return state


def latest_trade_price(symbol: str) -> float:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest

    key_id, secret_key = require_credentials()
    data_client = StockHistoricalDataClient(api_key=key_id, secret_key=secret_key)
    trades = data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
    return float(trades[symbol].price)


def non_marketable_limit_price(symbol: str) -> float:
    return round(latest_trade_price(symbol) * 0.5, 2)


def submit_once(client, request) -> tuple[dict | None, dict | None]:
    try:
        order = client.submit_order(request)
        return to_fields(order), None
    except Exception as exc:  # noqa: BLE001 - capture sanitized broker error
        return None, {
            "error_type": type(exc).__name__,
            "message": str(exc)[:500],
        }


def status_str(value) -> str:
    return str(getattr(value, "value", value))


def wait_terminal(client, order_id, attempts: int = 24, delay: float = 1.5) -> tuple[dict, list]:
    sequence = []
    fields = {}
    for _ in range(attempts):
        fields = to_fields(client.get_order_by_id(order_id))
        sequence.append(
            {
                "at_utc": utcnow().isoformat(timespec="seconds"),
                "status": status_str(fields.get("status")),
                "filled_qty": fields.get("filled_qty"),
                "filled_avg_price": fields.get("filled_avg_price"),
            }
        )
        if status_str(fields.get("status")) in TERMINAL_STATUSES:
            break
        time.sleep(delay)
    return fields, sequence


def position_qty(client, symbol: str) -> float:
    try:
        pos = client.get_open_position(symbol)
        return float(to_fields(pos).get("qty") or 0)
    except Exception:  # noqa: BLE001 - absent position is zero
        return 0.0


def cancel_and_wait(client, order_id) -> dict:
    client.cancel_order_by_id(order_id)
    final, _ = wait_terminal(client, order_id, attempts=12, delay=1.0)
    return final


def write_experiment_evidence(experiment: str, run_id: str, observations: dict) -> Path:
    observations = {"run_id": run_id, **observations}
    return write_evidence(experiment, observations)


def cmd_snapshot(args: argparse.Namespace) -> None:
    client = make_paper_client()
    state = preflight_guard(client)
    path = write_experiment_evidence("snapshot", args.run_id or default_run_id(), {"state": state})
    print(f"OK snapshot evidence -> {path}")


def cmd_b1_market_buy(args: argparse.Namespace) -> None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    client = make_paper_client()
    pre = preflight_guard(client)
    run_id = args.run_id or default_run_id()
    coid = client_order_id("b1_market_buy", "leg0", run_id)
    request = MarketOrderRequest(
        symbol="SGOV",
        qty=1,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        client_order_id=coid,
    )
    request_desc = {
        "symbol": "SGOV",
        "qty": 1,
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": coid,
    }
    order, submit_error = submit_once(client, request)
    final, lifecycle = ({}, [])
    if order is not None:
        final, lifecycle = wait_terminal(client, order["id"])
    post = get_state(client)
    sgov_qty = position_qty(client, "SGOV")
    gate = (
        submit_error is None
        and status_str(final.get("status")) == "filled"
        and float(final.get("filled_qty") or 0) == 1.0
        and sgov_qty >= 1.0
    )
    observations = {
        "pre_state": pre,
        "request": request_desc,
        "submit_error": submit_error,
        "initial_response": order,
        "lifecycle": lifecycle,
        "final_order": final,
        "post_state": post,
        "reconciliation": {"sgov_position_qty": sgov_qty, "gate_passed": gate},
        "conclusion": (
            "whole-share market buy filled and reconciled"
            if gate
            else "RECONCILIATION GATE FAILED — inspect evidence before continuing"
        ),
    }
    path = write_experiment_evidence("b1_market_buy", run_id, observations)
    print(f"{'OK' if gate else 'GATE FAILED'} b1 evidence -> {path}")
    if not gate:
        sys.exit(3)


def cmd_b2_limit_cancel(args: argparse.Namespace) -> None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest

    client = make_paper_client()
    pre = preflight_guard(client)
    run_id = args.run_id or default_run_id()
    coid = client_order_id("b2_limit_cancel", "leg0", run_id)
    limit_price = non_marketable_limit_price("SGOV")
    request = LimitOrderRequest(
        symbol="SGOV",
        qty=1,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        client_order_id=coid,
    )
    request_desc = {
        "symbol": "SGOV",
        "qty": 1,
        "side": "buy",
        "type": "limit",
        "limit_price": limit_price,
        "time_in_force": "day",
        "client_order_id": coid,
        "intent": "deliberately non-marketable (~50% of last trade) to observe cancel lifecycle",
    }
    order, submit_error = submit_once(client, request)
    open_sequence = []
    final_after_cancel = {}
    if order is not None:
        _, open_sequence = wait_terminal(client, order["id"], attempts=4, delay=1.0)
        final_after_cancel = cancel_and_wait(client, order["id"])
    post = get_state(client)
    gate = (
        submit_error is None
        and status_str(final_after_cancel.get("status")) == "canceled"
        and not post["open_orders"]
        and post["account"].get("cash") == pre["account"].get("cash")
        and position_qty(client, "SGOV") == position_qty_from_state(pre, "SGOV")
    )
    observations = {
        "pre_state": pre,
        "request": request_desc,
        "submit_error": submit_error,
        "initial_response": order,
        "open_phase_statuses": open_sequence,
        "final_order_after_cancel": final_after_cancel,
        "post_state": post,
        "reconciliation": {"gate_passed": gate},
        "conclusion": (
            "limit order canceled; account and positions unchanged"
            if gate
            else "GATE FAILED — inspect evidence before continuing"
        ),
    }
    path = write_experiment_evidence("b2_limit_cancel", run_id, observations)
    print(f"{'OK' if gate else 'GATE FAILED'} b2 evidence -> {path}")
    if not gate:
        sys.exit(3)


def position_qty_from_state(state: dict, symbol: str) -> float:
    for pos in state["positions"]:
        if pos.get("symbol") == symbol:
            return float(pos.get("qty") or 0)
    return 0.0


def cmd_b3_duplicate_id(args: argparse.Namespace) -> None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest

    client = make_paper_client()
    pre = preflight_guard(client)
    run_id = args.run_id or default_run_id()
    limit_price = non_marketable_limit_price("SGOV")

    def make_request(coid: str) -> LimitOrderRequest:
        return LimitOrderRequest(
            symbol="SGOV",
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
            client_order_id=coid,
        )

    coid = client_order_id("b3_duplicate_id", "leg0", run_id)
    first_order, first_error = submit_once(client, make_request(coid))
    second_order, second_error = submit_once(client, make_request(coid))

    broker_orders_with_id = []
    lookup_result = {}
    if first_order is not None:
        try:
            lookup_result = to_fields(client.get_order_by_client_id(coid))
        except Exception as exc:  # noqa: BLE001 - record lookup failure
            lookup_result = {"error": type(exc).__name__, "message": str(exc)[:300]}
        from alpaca.trading.requests import GetOrdersRequest

        broker_orders_with_id = [
            to_fields(o)
            for o in client.get_orders(GetOrdersRequest(status="open"))
            if o.client_order_id == coid
        ]

    id_constraint_probes = []
    for length in (48, 49, 64, 128):
        probe_coid = probe_client_order_id(length, "b3_duplicate_id", f"len{length}", run_id)
        probe_order, probe_error = submit_once(client, make_request(probe_coid))
        probe_entry = {
            "client_order_id_length": length,
            "accepted": probe_order is not None,
            "error": probe_error,
        }
        if probe_order is not None:
            cancel_and_wait(client, probe_order["id"])
            probe_entry["canceled_after_probe"] = True
        id_constraint_probes.append(probe_entry)

    final_after_cancel = {}
    if first_order is not None:
        final_after_cancel = cancel_and_wait(client, first_order["id"])
    post = get_state(client)
    single_broker_order = len(broker_orders_with_id) == 1 and second_order is None
    observations = {
        "pre_state": pre,
        "request": {
            "symbol": "SGOV",
            "qty": 1,
            "side": "buy",
            "type": "limit",
            "limit_price": limit_price,
            "time_in_force": "day",
            "client_order_id": coid,
            "intent": "identical client_order_id submitted twice; non-marketable limit",
        },
        "first_submission": {"order": first_order, "error": first_error},
        "second_submission_same_id": {"order": second_order, "error": second_error},
        "lookup_by_client_id": lookup_result,
        "open_orders_with_same_id_count": len(broker_orders_with_id),
        "id_constraint_probes": id_constraint_probes,
        "first_order_final_after_cancel": final_after_cancel,
        "post_state": post,
        "reconciliation": {"single_broker_order_for_logical_leg": single_broker_order},
        "conclusion": (
            "duplicate client_order_id did not create a second broker order"
            if single_broker_order
            else "DUPLICATE-RISK — inspect evidence before continuing"
        ),
    }
    path = write_experiment_evidence("b3_duplicate_id", run_id, observations)
    print(f"{'OK' if single_broker_order else 'DUPLICATE RISK'} b3 evidence -> {path}")
    if not single_broker_order:
        sys.exit(3)


def cmd_b4_fractional(args: argparse.Namespace) -> None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    client = make_paper_client()
    pre = preflight_guard(client)
    run_id = args.run_id or default_run_id()
    coid = client_order_id("b4_fractional", "leg0", run_id)
    request = MarketOrderRequest(
        symbol="SGOV",
        qty=0.1,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        client_order_id=coid,
    )
    request_desc = {
        "symbol": "SGOV",
        "qty": 0.1,
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": coid,
        "intent": "minimum-practical fractional quantity probe",
    }
    order, submit_error = submit_once(client, request)
    final, lifecycle = ({}, [])
    if order is not None:
        final, lifecycle = wait_terminal(client, order["id"])
    post = get_state(client)
    observations = {
        "pre_state": pre,
        "request": request_desc,
        "submit_error": submit_error,
        "initial_response": order,
        "lifecycle": lifecycle,
        "final_order": final,
        "post_state": post,
        "post_sgov_qty": position_qty(client, "SGOV"),
        "conclusion": "see evidence: fractional acceptance, lifecycle, resulting position",
    }
    path = write_experiment_evidence("b4_fractional", run_id, observations)
    print(f"OK b4 evidence -> {path}")


def cmd_b5_notional(args: argparse.Namespace) -> None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    client = make_paper_client()
    pre = preflight_guard(client)
    run_id = args.run_id or default_run_id()
    runs = []
    for leg, notional in (("probe_small", 0.5), ("main", 10)):
        coid = client_order_id("b5_notional", leg, run_id)
        request = MarketOrderRequest(
            symbol="SGOV",
            notional=notional,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            client_order_id=coid,
        )
        order, submit_error = submit_once(client, request)
        final, lifecycle = ({}, [])
        if order is not None:
            final, lifecycle = wait_terminal(client, order["id"])
        runs.append(
            {
                "leg": leg,
                "notional": notional,
                "client_order_id": coid,
                "submit_error": submit_error,
                "initial_response": order,
                "lifecycle": lifecycle,
                "final_order": final,
            }
        )
    post = get_state(client)
    observations = {
        "pre_state": pre,
        "request": {
            "symbol": "SGOV",
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "intent": "notional order probe: $0.50 minimum boundary then $10 main",
        },
        "runs": runs,
        "post_state": post,
        "post_sgov_qty": position_qty(client, "SGOV"),
        "conclusion": "see evidence: accepted notional form, lifecycle, resulting position",
    }
    path = write_experiment_evidence("b5_notional", run_id, observations)
    print(f"OK b5 evidence -> {path}")


def cmd_b6_unknown_recovery(args: argparse.Namespace) -> None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest

    client = make_paper_client()
    pre = preflight_guard(client)
    run_id = args.run_id or default_run_id()
    coid = client_order_id("b6_unknown_recovery", "leg0", run_id)
    limit_price = non_marketable_limit_price("SGOV")
    request = LimitOrderRequest(
        symbol="SGOV",
        qty=1,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        client_order_id=coid,
    )
    order, submit_error = submit_once(client, request)

    recovery = {"attempts": [], "found": False, "order": None}
    if order is not None:
        for attempt in range(1, 6):
            try:
                found = to_fields(client.get_order_by_client_id(coid))
                recovery["attempts"].append(
                    {"attempt": attempt, "at_utc": utcnow().isoformat(timespec="seconds"), "result": "found"}
                )
                recovery["found"] = True
                recovery["order"] = found
                break
            except Exception as exc:  # noqa: BLE001 - bounded retry with backoff
                recovery["attempts"].append(
                    {
                        "attempt": attempt,
                        "at_utc": utcnow().isoformat(timespec="seconds"),
                        "result": "not_found",
                        "error_type": type(exc).__name__,
                    }
                )
                time.sleep(min(attempt * 1.0, 5.0))

    final_after_cancel = {}
    if recovery["found"]:
        final_after_cancel = cancel_and_wait(client, recovery["order"]["id"])
    post = get_state(client)
    status = "RECOVERED" if recovery["found"] else "UNKNOWN_REQUIRES_REVIEW"
    observations = {
        "pre_state": pre,
        "request": {
            "symbol": "SGOV",
            "qty": 1,
            "side": "buy",
            "type": "limit",
            "limit_price": limit_price,
            "time_in_force": "day",
            "client_order_id": coid,
            "intent": "non-marketable limit; local confirmation discarded after single submission",
        },
        "submit_error": submit_error,
        "local_confirmation": "discarded_by_design",
        "recovery": recovery,
        "second_submissions_made": 0,
        "final_order_after_cancel": final_after_cancel,
        "post_state": post,
        "status": status,
        "conclusion": (
            "recovered by client_order_id with zero resubmissions"
            if recovery["found"]
            else "UNKNOWN_REQUIRES_REVIEW — no automatic resubmission; human resolution required"
        ),
    }
    path = write_experiment_evidence("b6_unknown_recovery", run_id, observations)
    print(f"{'OK' if recovery['found'] else 'UNKNOWN_REQUIRES_REVIEW'} b6 evidence -> {path}")
    if not recovery["found"]:
        sys.exit(4)


def cmd_b7_settlement_sell(args: argparse.Namespace) -> None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    client = make_paper_client()
    pre = preflight_guard(client)
    run_id = args.run_id or default_run_id()
    sells = []
    skipped = []
    for pos in pre["positions"]:
        symbol = pos.get("symbol")
        qty = float(pos.get("qty") or 0)
        if symbol not in ASSET_UNIVERSE:
            skipped.append({"symbol": symbol, "reason": "outside spike universe; left for human review"})
            continue
        if qty <= 0:
            skipped.append({"symbol": symbol, "reason": "non-positive qty; refusing (CHECK-16 long-only)"})
            continue
        coid = client_order_id("b7_settlement_sell", f"leg_{symbol}", run_id)
        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=coid,
        )
        order, submit_error = submit_once(client, request)
        final, lifecycle = ({}, [])
        if order is not None:
            final, lifecycle = wait_terminal(client, order["id"])
        cash_immediately = None
        try:
            cash_immediately = to_fields(client.get_account()).get("cash")
        except Exception:  # noqa: BLE001 - non-fatal observation
            pass
        sells.append(
            {
                "symbol": symbol,
                "qty_sold": qty,
                "client_order_id": coid,
                "submit_error": submit_error,
                "initial_response": order,
                "lifecycle": lifecycle,
                "final_order": final,
                "account_cash_immediately_after_terminal": cash_immediately,
            }
        )
    time.sleep(5)
    delayed_account = {
        k: v
        for k, v in to_fields(client.get_account()).items()
        if k not in ACCOUNT_REDACTIONS
    }
    post = get_state(client)
    observations = {
        "pre_state": pre,
        "sells": sells,
        "skipped": skipped,
        "delayed_account_5s": delayed_account,
        "post_state": post,
        "settlement_note": (
            "Alpaca paper cash crediting observed only; Opaca derives T+1 availability "
            "independently from its business-day calendar and does not treat broker "
            "crediting as legal/operational settlement"
        ),
        "conclusion": "see evidence: sell lifecycle, position changes, cash crediting timing",
    }
    path = write_experiment_evidence("b7_settlement_sell", run_id, observations)
    print(f"OK b7 evidence -> {path}")


def cmd_b8_extended_hours(args: argparse.Namespace) -> None:
    client = make_paper_client()
    run_id = args.run_id or default_run_id()
    flags = {}
    for symbol in ASSET_UNIVERSE:
        asset = to_fields(client.get_asset(symbol))
        flags[symbol] = {
            "attributes": asset.get("attributes"),
            "fractionable": asset.get("fractionable"),
            "tradable": asset.get("tradable"),
        }
    from alpaca.trading.requests import LimitOrderRequest

    clock_fields = to_fields(client.get_clock())
    observations = {
        "asset_flags": flags,
        "request_model_support": {
            "extended_hours_field_present": "extended_hours" in LimitOrderRequest.model_fields,
            "note": "extended-hours orders require limit type per Alpaca semantics; verify empirically before use",
        },
        "market_state_at_check": {"is_open": clock_fields.get("is_open")},
        "status": "UNVERIFIED / NON-BLOCKING",
        "conclusion": (
            "outside-RTH live test not possible while market is open; asset attributes and "
            "request semantics recorded; treat extended hours as unverified and non-blocking"
        ),
    }
    path = write_experiment_evidence("b8_extended_hours", run_id, observations)
    print(f"OK b8 evidence -> {path}")


def add_run_id_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id", default=None, help="deterministic run identifier (default: <UTC date>-r1)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Opaca Phase −1 broker reality spike (read-only experiments only)"
    )
    sub = parser.add_subparsers(dest="experiment", required=True)
    sub.add_parser("account", help="A1: read-only account reality").set_defaults(fn=cmd_account)
    sub.add_parser("assets", help="A2: read-only asset tradability").set_defaults(fn=cmd_assets)
    sub.add_parser("clock", help="A3: read-only market clock").set_defaults(fn=cmd_clock)
    sub.add_parser("calendar", help="A4: read-only trading calendar").set_defaults(fn=cmd_calendar)
    mutating = [
        ("snapshot", "fresh read-only account/positions/orders snapshot", cmd_snapshot),
        ("b1-market-buy", "B1: whole-share market buy (1 share SGOV)", cmd_b1_market_buy),
        ("b2-limit-cancel", "B2: non-marketable limit buy then cancel", cmd_b2_limit_cancel),
        ("b3-duplicate-id", "B3: duplicate client_order_id behavior", cmd_b3_duplicate_id),
        ("b4-fractional", "B4: fractional quantity buy (0.1 SGOV)", cmd_b4_fractional),
        ("b5-notional", "B5: notional order ($0.50 probe, $10 main)", cmd_b5_notional),
        ("b6-unknown-recovery", "B6: UNKNOWN/crash recovery by client_order_id", cmd_b6_unknown_recovery),
        ("b7-settlement-sell", "B7: sell reconciled longs; observe cash crediting", cmd_b7_settlement_sell),
        ("b8-extended-hours", "B8: extended-hours flags only (non-mutating)", cmd_b8_extended_hours),
    ]
    for name, help_text, fn in mutating:
        p = sub.add_parser(name, help=help_text)
        add_run_id_arg(p)
        p.set_defaults(fn=fn)
    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
