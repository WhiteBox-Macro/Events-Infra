#!/usr/bin/env python3
"""macro_alpha_vantage.py — Alpha Vantage macro-indicator ingester.

Periodically pulls value-history for a fixed set of high-importance series:
CPI, Core CPI proxy, unemployment, non-farm payrolls, fed funds, real GDP,
retail sales, treasury yields.

Two important caveats baked into the schema:

  1. Alpha Vantage returns the period date (e.g. 2026-04-01 for April CPI)
     but NOT the actual release timestamp (which is what trades on). We set
     `released_at` to the period date here so backtest replays land on the
     right month; the Fed / BLS RSS feeds catch the real-time release event
     separately and fire the dispatcher's macro_in NOTIFY.

  2. AV macro endpoints return the full history every call. We only insert
     rows newer than max(period_start) per indicator, so subsequent ticks
     are cheap.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dbkit import pg  # noqa: E402
from dbkit.constants import load_dotenv_files  # noqa: E402
from dbkit.http import HttpRetryError, request_json  # noqa: E402
from scripts.ingest._common import (  # noqa: E402
    persist_raw_payload,
    poll_loop,
    setup_logging,
    singleton_lock,
    stop_event,
)

INGESTER_NAME = "macro_alpha_vantage"
AV_URL = "https://www.alphavantage.co/query"
POLL_INTERVAL_SEC = 6 * 3600   # 6h — macro doesn't change minute-to-minute
INTER_CALL_DELAY_SEC = 13       # ~5 calls/min under premium; safe under free 25/day

# Indicator code → (AV function, params) + default registry metadata used
# to bootstrap macro.indicators rows on first run.
INDICATOR_SPECS = {
    "CPI":              {"function": "CPI",               "params": {"interval": "monthly"},
                         "name": "CPI, all urban consumers",      "units": "index_1982_84_100",  "frequency": "monthly", "importance": 5},
    "INFLATION":        {"function": "INFLATION",         "params": {},
                         "name": "Inflation (annual)",            "units": "percent",            "frequency": "annual",  "importance": 4},
    "UNRATE":           {"function": "UNEMPLOYMENT",      "params": {},
                         "name": "Unemployment rate",             "units": "percent",            "frequency": "monthly", "importance": 5},
    "PAYEMS":           {"function": "NONFARM_PAYROLL",   "params": {},
                         "name": "Non-farm payrolls",             "units": "thousands",          "frequency": "monthly", "importance": 5},
    "FEDFUNDS":         {"function": "FEDERAL_FUNDS_RATE","params": {"interval": "monthly"},
                         "name": "Effective federal funds rate",  "units": "percent",            "frequency": "monthly", "importance": 5},
    "GDP":              {"function": "REAL_GDP",          "params": {"interval": "quarterly"},
                         "name": "Real GDP",                      "units": "billions_chained",   "frequency": "quarterly","importance": 4},
    "RETAIL_SALES":     {"function": "RETAIL_SALES",      "params": {},
                         "name": "Retail sales",                  "units": "millions",           "frequency": "monthly", "importance": 4},
    "DURABLES":         {"function": "DURABLES",          "params": {},
                         "name": "Durable goods orders",          "units": "millions",           "frequency": "monthly", "importance": 3},
    "TREASURY_10Y":     {"function": "TREASURY_YIELD",    "params": {"interval": "daily", "maturity": "10year"},
                         "name": "10-year treasury yield",        "units": "percent",            "frequency": "daily",   "importance": 4},
    "TREASURY_2Y":      {"function": "TREASURY_YIELD",    "params": {"interval": "daily", "maturity": "2year"},
                         "name": "2-year treasury yield",         "units": "percent",            "frequency": "daily",   "importance": 4},
}


def _ensure_indicator(code: str, spec: dict) -> dict:
    """Create the macro.indicators row if missing; return current row."""
    row = pg.query("macro.indicators", where={"code": code}, limit=1)
    if row:
        return row[0]
    pg.upsert(
        "macro.indicators",
        {
            "code": code,
            "name": spec["name"],
            "source": "alpha_vantage",
            "source_series": spec["function"],
            "units": spec["units"],
            "frequency": spec["frequency"],
            "importance": spec["importance"],
            "metadata": {"av_params": spec["params"]},
        },
        conflict_on=["code"],
    )
    return pg.query("macro.indicators", where={"code": code}, limit=1)[0]


def _max_period(indicator_id: int) -> datetime | None:
    rows = pg.execute(
        "SELECT MAX(period_start) AS m FROM macro.releases WHERE indicator_id = %s",
        [indicator_id],
    )
    m = rows[0]["m"] if rows else None
    if m is None:
        return None
    if isinstance(m, datetime):
        return m
    # Postgres returns DATE here; convert to datetime at midnight UTC.
    return datetime.combine(m, datetime.min.time(), tzinfo=timezone.utc)


def _av_call(function: str, extra: dict) -> dict:
    api_key = os.environ.get("ALPHA_VANTAGE_API_KEY")
    if not api_key:
        raise RuntimeError("ALPHA_VANTAGE_API_KEY not set")
    params = {"function": function, "apikey": api_key}
    params.update(extra or {})
    payload = request_json("GET", AV_URL, params=params, timeout=30, max_attempts=4)
    if isinstance(payload, dict) and "Note" in payload:
        raise RuntimeError(f"Alpha Vantage rate limited: {payload['Note']}")
    if isinstance(payload, dict) and "Information" in payload and "data" not in payload:
        raise RuntimeError(f"Alpha Vantage info-only response: {payload['Information']}")
    return payload


def _parse_period(raw: str) -> datetime | None:
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


def _as_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (TypeError, InvalidOperation):
        return None


def _ingest_indicator(code: str, spec: dict, log) -> int:
    indicator = _ensure_indicator(code, spec)
    payload = _av_call(spec["function"], spec["params"])
    persist_raw_payload("alpha_vantage_macro", code, payload)

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return 0

    cutoff = _max_period(indicator["id"])
    inserted = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        period_dt = _parse_period(item.get("date", ""))
        if not period_dt:
            continue
        if cutoff and period_dt <= cutoff:
            continue
        value = _as_decimal(item.get("value"))
        if value is None:
            continue
        row = {
            "indicator_id": indicator["id"],
            "period_start": period_dt.date(),
            "period_end": None,
            "value": value,
            "prior_value": None,
            "consensus": None,
            "surprise": None,
            "surprise_z": None,
            # AV gives period not release timestamp; set released_at = period
            # midnight UTC. The dispatcher's "first to react" path leans on
            # news.articles (Fed/BLS RSS) for live release timing.
            "released_at": period_dt,
            "is_revision": False,
            "metadata": {"av_unit": payload.get("unit"), "av_interval": payload.get("interval")},
        }
        try:
            pg.upsert(
                "macro.releases", row,
                conflict_on=["indicator_id", "period_start", "is_revision", "released_at"],
            )
            inserted += 1
        except Exception:
            log.exception("upsert failed for %s @ %s", code, period_dt)
    if inserted:
        log.info("%s: inserted %d new releases", code, inserted)
    return inserted


def tick() -> None:
    log = setup_logging(INGESTER_NAME)
    for i, (code, spec) in enumerate(INDICATOR_SPECS.items()):
        if stop_event.is_set():
            return
        if i > 0:
            time.sleep(INTER_CALL_DELAY_SEC)
        try:
            _ingest_indicator(code, spec, log)
        except Exception:
            log.exception("indicator %s failed", code)


def main() -> int:
    load_dotenv_files()
    setup_logging(INGESTER_NAME)
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2
    with singleton_lock(INGESTER_NAME):
        poll_loop(name=INGESTER_NAME, tick_fn=tick, tick_interval_sec=POLL_INTERVAL_SEC)
    return 0


if __name__ == "__main__":
    sys.exit(main())
