#!/usr/bin/env python3
"""
Fetch intraday market data from Yahoo Finance and store as local parquet files.

Yahoo API limits:
    1m  → last 7 calendar days only
    1h  → last 730 days
    1d  → full history

Usage:
    # Fetch core futures at all available intervals
    python fetch_yf.py --start 2024-10-01

    # Fetch specific tickers
    python fetch_yf.py --tickers AAPL MSFT NVDA --start 2024-10-01

    # Fetch tickers found in the events datalake
    python fetch_yf.py --from-datalake --start 2024-10-01

    # Only 1h interval
    python fetch_yf.py --start 2024-10-01 --intervals 1h

    # Dry run (show what would be fetched)
    python fetch_yf.py --start 2024-10-01 --dry-run
"""

import argparse
import logging
import os
import sys
import json
import glob as glob_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

CORE_FUTURES = {
    "ES=F": "E-mini S&P 500",
    "NQ=F": "E-mini Nasdaq 100",
    "CL=F": "WTI Crude Oil",
    "GC=F": "Gold",
}

# Yahoo API hard limits per interval
INTERVAL_MAX_DAYS = {
    "1m": 7,
    "5m": 60,
    "15m": 60,
    "1h": 730,
    "1d": 10000,
}

# Chunk sizes for fetching (stay within API limits with margin)
INTERVAL_CHUNK_DAYS = {
    "1m": 6,
    "5m": 55,
    "15m": 55,
    "1h": 59,
    "1d": 365,
}


def get_data_dir() -> Path:
    base = os.environ.get("DB_BASE", os.path.expanduser("~/aotc-signals-data"))
    return Path(base) / "events" / "market_data"


def safe_ticker_dir(ticker: str) -> str:
    return ticker.replace("=", "_").replace("/", "_").replace("^", "_")


def output_path(data_dir: Path, ticker: str, interval: str, year: int, month: int) -> Path:
    tdir = safe_ticker_dir(ticker)
    d = data_dir / tdir / interval
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{tdir}_{interval}_{year:04d}-{month:02d}.parquet"


def fetch_chunk(ticker: str, start: datetime, end: datetime, interval: str) -> pd.DataFrame | None:
    try:
        t = yf.Ticker(ticker)
        df = t.history(start=start, end=end, interval=interval, auto_adjust=True)
        if df is None or df.empty:
            return None
        df = df.reset_index()
        date_col = "Datetime" if "Datetime" in df.columns else "Date"
        df = df.rename(columns={
            date_col: "timestamp",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        })
        keep = ["timestamp", "open", "high", "low", "close", "volume"]
        df = df[[c for c in keep if c in df.columns]].copy()

        if df["timestamp"].dt.tz is None:
            df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
        else:
            df["timestamp"] = df["timestamp"].dt.tz_convert("UTC")

        df["ticker"] = ticker
        df["interval"] = interval
        return df
    except Exception as e:
        log.warning("Failed %s %s [%s→%s]: %s", ticker, interval, start.date(), end.date(), e)
        return None


def generate_month_ranges(start: datetime, end: datetime):
    cursor = start.replace(day=1)
    while cursor < end:
        month_end = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        chunk_end = min(month_end, end)
        yield cursor, chunk_end
        cursor = month_end


def fetch_ticker_interval(ticker: str, start: datetime, end: datetime, interval: str, data_dir: Path, dry_run: bool = False) -> int:
    max_days = INTERVAL_MAX_DAYS[interval]
    earliest = datetime.now(timezone.utc) - timedelta(days=max_days)
    effective_start = max(start, earliest)

    if effective_start >= end:
        log.info("  %s %s: entire range outside API limit (%dd), skipping", ticker, interval, max_days)
        return 0

    if effective_start > start:
        log.info("  %s %s: API limit %dd, trimming start from %s to %s",
                 ticker, interval, max_days, start.date(), effective_start.date())

    chunk_days = INTERVAL_CHUNK_DAYS[interval]
    rows_total = 0

    if interval in ("1m", "5m", "15m"):
        cursor = effective_start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=chunk_days), end)
            if dry_run:
                log.info("  [DRY] %s %s %s→%s", ticker, interval, cursor.date(), chunk_end.date())
                cursor = chunk_end
                continue

            df = fetch_chunk(ticker, cursor, chunk_end, interval)
            if df is not None and not df.empty:
                for (year, month), group in df.groupby([df["timestamp"].dt.year, df["timestamp"].dt.month]):
                    out = output_path(data_dir, ticker, interval, year, month)
                    if out.exists():
                        existing = pd.read_parquet(out)
                        group = pd.concat([existing, group]).drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
                    group.to_parquet(out, index=False, engine="pyarrow")
                    rows_total += len(group)
                    log.info("  %s %s %04d-%02d: %d rows → %s", ticker, interval, year, month, len(group), out.name)
            cursor = chunk_end
    else:
        for month_start, month_end in generate_month_ranges(effective_start, end):
            if dry_run:
                log.info("  [DRY] %s %s %s→%s", ticker, interval, month_start.date(), month_end.date())
                continue

            chunk_start = month_start
            while chunk_start < month_end:
                chunk_end = min(chunk_start + timedelta(days=chunk_days), month_end)
                df = fetch_chunk(ticker, chunk_start, chunk_end, interval)
                if df is not None and not df.empty:
                    year, month = month_start.year, month_start.month
                    out = output_path(data_dir, ticker, interval, year, month)
                    if out.exists():
                        existing = pd.read_parquet(out)
                        df = pd.concat([existing, df]).drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
                    df.to_parquet(out, index=False, engine="pyarrow")
                    rows_total += len(df)
                    log.info("  %s %s %04d-%02d: %d rows → %s", ticker, interval, year, month, len(df), out.name)
                chunk_start = chunk_end

    return rows_total


def collect_datalake_tickers(data_dir: Path) -> set[str]:
    raw_base = os.environ.get("DB_BASE", os.path.expanduser("~/aotc-signals-data"))
    raw_dir = Path(raw_base) / "events" / "raw"
    tickers = set()

    if not raw_dir.exists():
        log.warning("Datalake not found at %s", raw_dir)
        return tickers

    for json_path in raw_dir.rglob("*.json"):
        try:
            with open(json_path) as f:
                data = json.load(f)
            payload = data.get("payload", data)
            for field in ("tickers", "ticker_mentions", "symbols"):
                val = payload.get(field)
                if isinstance(val, list):
                    tickers.update(t for t in val if isinstance(t, str) and 1 <= len(t) <= 10)
                elif isinstance(val, str) and val:
                    tickers.add(val)
        except (json.JSONDecodeError, OSError):
            continue

    log.info("Found %d unique tickers in datalake", len(tickers))
    return tickers


def main():
    parser = argparse.ArgumentParser(description="Fetch intraday market data from Yahoo Finance")
    parser.add_argument("--tickers", nargs="+", help="Ticker symbols to fetch (default: core futures)")
    parser.add_argument("--from-datalake", action="store_true", help="Also fetch tickers found in events datalake")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="End date (YYYY-MM-DD, default: today)")
    parser.add_argument("--intervals", nargs="+", default=["1h", "1m"],
                        choices=list(INTERVAL_MAX_DAYS.keys()),
                        help="Intervals to fetch (default: 1h 1m)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fetched")
    parser.add_argument("--data-dir", help="Override output directory")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc)
    data_dir = Path(args.data_dir) if args.data_dir else get_data_dir()

    tickers = set()
    if args.tickers:
        tickers.update(args.tickers)
    else:
        tickers.update(CORE_FUTURES.keys())

    if args.from_datalake:
        tickers.update(collect_datalake_tickers(data_dir))

    log.info("Tickers: %s", sorted(tickers))
    log.info("Range: %s → %s", start.date(), end.date())
    log.info("Intervals: %s", args.intervals)
    log.info("Output: %s", data_dir)

    total_rows = 0
    for ticker in sorted(tickers):
        log.info("── %s (%s) ──", ticker, CORE_FUTURES.get(ticker, ""))
        for interval in args.intervals:
            rows = fetch_ticker_interval(ticker, start, end, interval, data_dir, args.dry_run)
            total_rows += rows

    log.info("Done. %d total rows across %d tickers.", total_rows, len(tickers))


if __name__ == "__main__":
    main()
