#!/usr/bin/env python3
"""
Fetch 1-min OHLCV from IBKR Web API (Client Portal Gateway).
Much faster than TWS API: 10 req/sec vs 6 req/min.

Prerequisites:
    1. Download Client Portal Gateway from IBKR
    2. Start: bin/run.bat root/conf.yaml
    3. Authenticate: open https://localhost:5000 in browser, log in
    4. Run this script

Usage:
    python fetch_webapi.py --start 2024-10-01
    python fetch_webapi.py --tickers SPY QQQ --start 2024-10-01 --workers 3
    python fetch_webapi.py --start 2024-10-01 --dry-run
"""

import argparse
import json
import logging
import os
import time
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DEFAULT_TICKERS = ["SPY", "QQQ", "GLD", "USO"]

DATALAKE_TICKERS = [
    "NVDA", "TSLA", "DJT", "AMZN", "AAPL", "MSFT", "TSLL", "ORCL",
    "GOOG", "META", "INTC", "CRCL", "GOOGL", "SMCI", "MSTR", "COIN",
    "PLTR", "BA", "TSM", "NFLX", "GME", "JPM", "AVGO", "IBIT",
    "AMD", "HIMS", "CRWV", "NVO", "UNH", "SFTBY",
]

BASE_URL = "https://localhost:5000/v1/api"
PACING_SECONDS = 0.15  # 10 req/sec limit → 0.1s + margin


def get_data_dir() -> Path:
    base = os.environ.get("DB_BASE", os.path.expanduser("~/aotc-signals-data"))
    return Path(base) / "events" / "market_data"


def output_path(data_dir: Path, ticker: str, year: int, month: int) -> Path:
    d = data_dir / ticker / "1m"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{ticker}_1m_{year:04d}-{month:02d}.parquet"


def has_month(data_dir: Path, ticker: str, year: int, month: int) -> bool:
    return output_path(data_dir, ticker, year, month).exists()


def save_month(path: Path, df: pd.DataFrame) -> int:
    if path.exists():
        existing = pd.read_parquet(path)
        df = pd.concat([existing, df]).drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
    df.to_parquet(path, index=False, engine="pyarrow")
    return len(df)


# ── Auth & contract resolution ───────────────────────────────────────

def check_auth(session: requests.Session) -> bool:
    try:
        r = session.get(f"{BASE_URL}/iserver/auth/status", verify=False, timeout=5)
        data = r.json()
        if data.get("authenticated"):
            log.info("Authenticated as %s", data.get("competing", "unknown"))
            return True
        log.error("Not authenticated. Open https://localhost:5000 and log in.")
        return False
    except Exception as e:
        log.error("Cannot reach Web API: %s", e)
        log.error("Start Client Portal Gateway and authenticate at https://localhost:5000")
        return False


def resolve_conid(session: requests.Session, ticker: str) -> int | None:
    """Resolve ticker symbol to IBKR conid via search."""
    try:
        r = session.get(
            f"{BASE_URL}/iserver/secdef/search",
            params={"symbol": ticker, "secType": "STK"},
            verify=False, timeout=10,
        )
        results = r.json()
        if results and isinstance(results, list):
            return results[0].get("conid")
    except Exception as e:
        log.warning("Failed to resolve %s: %s", ticker, e)
    return None


# ── Historical data fetch ────────────────────────────────────────────

def fetch_period(session: requests.Session, conid: int, end_dt: str, period: str = "1d") -> pd.DataFrame:
    """
    Fetch 1-min bars for a period.
    period: "1d", "2d", "3d", "5d", "1w"
    end_dt: "20250305-23:59:59" format
    """
    # Web API uses different date format and params
    params = {
        "conid": conid,
        "period": period,
        "bar": "1min",
        "outsideRth": "true",
        "startTime": end_dt,  # confusingly named, acts as reference point
    }

    for attempt in range(3):
        try:
            r = session.get(
                f"{BASE_URL}/iserver/marketdata/history",
                params=params,
                verify=False,
                timeout=30,
            )
            if r.status_code == 429:
                wait = 10 * (attempt + 1)
                log.warning("Rate limited, waiting %ds (%d/3)", wait, attempt + 1)
                time.sleep(wait)
                continue

            if r.status_code != 200:
                log.warning("HTTP %d for conid %d: %s", r.status_code, conid, r.text[:100])
                time.sleep(1)
                continue

            data = r.json()
            bars = data.get("data", [])
            if not bars:
                return pd.DataFrame()

            rows = []
            for b in bars:
                ts = pd.Timestamp(b["t"], unit="ms", tz="UTC")
                rows.append({
                    "timestamp": ts,
                    "open": b["o"],
                    "high": b["h"],
                    "low": b["l"],
                    "close": b["c"],
                    "volume": int(b.get("v", 0)),
                })
            return pd.DataFrame(rows)

        except Exception as e:
            log.warning("Error fetching conid %d: %s (%d/3)", conid, e, attempt + 1)
            time.sleep(1)

    return pd.DataFrame()


def fetch_ticker_range(session: requests.Session, ticker: str, conid: int,
                       start: datetime, end: datetime, data_dir: Path) -> int:
    """Fetch all days for one ticker, save by month."""
    buf: dict[tuple, list] = {}
    total_rows = 0
    cursor = start

    while cursor <= end:
        if cursor.weekday() >= 5:
            cursor += timedelta(days=1)
            continue

        end_str = cursor.strftime("%Y%m%d-23:59:59")
        df = fetch_period(session, conid, end_str, period="1d")
        if not df.empty:
            key = (cursor.year, cursor.month)
            buf.setdefault(key, []).append(df)
            total_rows += len(df)

        # Flush completed months
        next_day = cursor + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        if next_day.month != cursor.month and (cursor.year, cursor.month) in buf:
            key = (cursor.year, cursor.month)
            merged = pd.concat(buf.pop(key))
            out = output_path(data_dir, ticker, key[0], key[1])
            n = save_month(out, merged)
            log.info("  %s 1m %04d-%02d: %d rows", ticker, key[0], key[1], n)

        cursor += timedelta(days=1)
        time.sleep(PACING_SECONDS)

    for key, frames in buf.items():
        merged = pd.concat(frames)
        out = output_path(data_dir, ticker, key[0], key[1])
        n = save_month(out, merged)
        log.info("  %s 1m %04d-%02d: %d rows", ticker, key[0], key[1], n)

    return total_rows


def expand_work(tickers: list[str], start: datetime, end: datetime, data_dir: Path) -> list[str]:
    """Filter to tickers that still need data."""
    work = []
    for ticker in tickers:
        cursor = start
        needs_data = False
        while cursor <= end:
            if not has_month(data_dir, ticker, cursor.year, cursor.month):
                needs_data = True
                break
            if cursor.month == 12:
                cursor = cursor.replace(year=cursor.year + 1, month=1, day=1)
            else:
                cursor = cursor.replace(month=cursor.month + 1, day=1)
        if needs_data:
            work.append(ticker)
        else:
            log.info("%s: all months exist, skipping", ticker)
    return work


def main():
    parser = argparse.ArgumentParser(description="Fetch 1-min data via IBKR Web API")
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS + DATALAKE_TICKERS)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", default=None)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    global BASE_URL
    BASE_URL = args.base_url

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc)
    data_dir = Path(args.data_dir) if args.data_dir else get_data_dir()

    session = requests.Session()
    if not check_auth(session):
        return

    # Filter tickers that need data
    work = expand_work(args.tickers, start, end, data_dir)
    if not work:
        log.info("Nothing to fetch.")
        return

    # Resolve conids
    log.info("Resolving %d tickers...", len(work))
    conid_map = {}
    for ticker in work:
        conid = resolve_conid(session, ticker)
        if conid:
            conid_map[ticker] = conid
            log.info("  %s → conid %d", ticker, conid)
        else:
            log.warning("  %s → NOT FOUND, skipping", ticker)
        time.sleep(PACING_SECONDS)

    log.info("Fetching %d tickers, %d workers", len(conid_map), args.workers)
    log.info("Range: %s → %s", start.date(), end.date())
    log.info("Output: %s", data_dir)

    if args.dry_run:
        for ticker in conid_map:
            days = sum(1 for d in range((end - start).days + 1)
                       if (start + timedelta(days=d)).weekday() < 5)
            log.info("  [DRY] %s (conid %d): %d trading days", ticker, conid_map[ticker], days)
        return

    grand_total = 0
    if args.workers <= 1:
        for ticker, conid in conid_map.items():
            log.info("── %s ──", ticker)
            rows = fetch_ticker_range(session, ticker, conid, start, end, data_dir)
            grand_total += rows
            log.info("  %s: %d rows", ticker, rows)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {}
            for ticker, conid in conid_map.items():
                f = pool.submit(fetch_ticker_range, session, ticker, conid, start, end, data_dir)
                futures[f] = ticker
            for f in as_completed(futures):
                ticker = futures[f]
                try:
                    rows = f.result()
                    grand_total += rows
                    log.info("=== %s complete: %d rows ===", ticker, rows)
                except Exception as e:
                    log.error("=== %s failed: %s ===", ticker, e)

    log.info("Done. %d total rows.", grand_total)


if __name__ == "__main__":
    main()
