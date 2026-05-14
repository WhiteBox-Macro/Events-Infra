#!/usr/bin/env python3
"""
Scan the datalake (raw tweet JSONs) for ticker mentions via cashtags,
then fetch 1-min OHLCV (including premarket) from IBKR for each.

Skips tickers that already have local data.

Usage:
    python fetch_ibkr_datalake.py --start 2024-10-01
    python fetch_ibkr_datalake.py --start 2024-10-01 --top 50
    python fetch_ibkr_datalake.py --start 2024-10-01 --dry-run
"""

import argparse
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from ib_insync import IB, Stock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

PACING_SECONDS = 2.5
SKIP_TICKERS = {"SPY", "QQQ", "GLD", "USO"}


def get_data_dir() -> Path:
    base = os.environ.get("DB_BASE", os.path.expanduser("~/aotc-signals-data"))
    return Path(base) / "events" / "market_data"


def get_raw_dir() -> Path:
    base = os.environ.get("DB_BASE", os.path.expanduser("~/aotc-signals-data"))
    return Path(base) / "events" / "raw"


def output_path(data_dir: Path, ticker: str, year: int, month: int) -> Path:
    d = data_dir / ticker / "1m"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{ticker}_1m_{year:04d}-{month:02d}.parquet"


def has_data(data_dir: Path, ticker: str) -> bool:
    d = data_dir / ticker / "1m"
    return d.exists() and any(d.glob("*.parquet"))


def extract_tickers_from_tweet(data: dict) -> set[str]:
    """Extract cashtag tickers from both X API v2 and twitterapi.io formats."""
    tickers = set()
    payload = data.get("payload", data)

    # X API v2 format: entities.cashtags[].tag
    entities = payload.get("entities", {})
    for ct in entities.get("cashtags", []):
        tag = ct.get("tag", "")
        if tag and 1 <= len(tag) <= 6 and tag.isalpha() and tag.isupper():
            tickers.add(tag)

    # twitterapi.io format: entities.symbols[].text
    for sym in entities.get("symbols", []):
        text = sym.get("text", "")
        if text and 1 <= len(text) <= 6 and text.isalpha() and text.isupper():
            tickers.add(text)

    # Fallback: regex cashtags from tweet text
    text = payload.get("text", "")
    if text:
        for m in re.findall(r'\$([A-Z]{1,6})\b', text):
            tickers.add(m)

    return tickers


def scan_datalake(raw_dir: Path) -> Counter:
    """Walk all raw JSON files, extract ticker mentions."""
    counts = Counter()
    file_count = 0

    if not raw_dir.exists():
        log.warning("Datalake not found at %s", raw_dir)
        return counts

    for json_path in raw_dir.rglob("*.json"):
        try:
            with open(json_path, encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            tickers = extract_tickers_from_tweet(data)
            counts.update(tickers)
            file_count += 1
        except (json.JSONDecodeError, OSError):
            continue

    log.info("Scanned %d files, found %d unique tickers", file_count, len(counts))
    return counts


def fetch_day(ib: IB, contract, end_dt: datetime) -> pd.DataFrame:
    end_str = end_dt.strftime("%Y%m%d-%H:%M:%S")
    for attempt in range(3):
        try:
            bars = ib.reqHistoricalData(
                contract,
                endDateTime=end_str,
                durationStr="1 D",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
                timeout=30,
            )
            if not bars:
                return pd.DataFrame()
            rows = []
            for b in bars:
                ts = pd.Timestamp(b.date)
                ts = ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")
                rows.append({
                    "timestamp": ts,
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": int(b.volume),
                })
            return pd.DataFrame(rows)
        except Exception as e:
            if "pacing" in str(e).lower():
                time.sleep(15 * (attempt + 1))
            else:
                log.warning("Fetch error: %s (%d/3)", e, attempt + 1)
                time.sleep(PACING_SECONDS)
    return pd.DataFrame()


def save_month(path: Path, df: pd.DataFrame):
    if path.exists():
        existing = pd.read_parquet(path)
        df = pd.concat([existing, df]).drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
    df.to_parquet(path, index=False, engine="pyarrow")
    return len(df)


def fetch_ticker(ib: IB, ticker: str, start: datetime, end: datetime, data_dir: Path) -> int:
    contract = Stock(ticker, "SMART", "USD")
    try:
        if not ib.qualifyContracts(contract):
            log.warning("  %s: not found on IBKR, skipping", ticker)
            return 0
    except Exception as e:
        log.warning("  %s: qualify failed: %s", ticker, e)
        return 0

    buf: dict[tuple, list] = {}
    total_rows = 0
    cursor = start

    while cursor <= end:
        if cursor.weekday() >= 5:
            cursor += timedelta(days=1)
            continue

        df = fetch_day(ib, contract, cursor.replace(hour=23, minute=59, second=59))
        if not df.empty:
            key = (cursor.year, cursor.month)
            buf.setdefault(key, []).append(df)
            total_rows += len(df)

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", default=None)
    parser.add_argument("--top", type=int, default=0, help="Top N tickers by mention count (0=all)")
    parser.add_argument("--port", type=int, default=4001)
    parser.add_argument("--client-id", type=int, default=55)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc)
    data_dir = Path(args.data_dir) if args.data_dir else get_data_dir()
    raw_dir = get_raw_dir()

    # Scan datalake
    log.info("Scanning datalake at %s ...", raw_dir)
    counts = scan_datalake(raw_dir)

    if not counts:
        log.info("No tickers found.")
        return

    # Filter out already-fetched
    for skip in SKIP_TICKERS:
        counts.pop(skip, None)

    ranked = counts.most_common(args.top if args.top > 0 else None)
    already = [(t, c) for t, c in ranked if has_data(data_dir, t)]
    to_fetch = [(t, c) for t, c in ranked if not has_data(data_dir, t)]

    log.info("Top tickers by mention count:")
    for t, c in ranked[:30]:
        status = "SKIP (has data)" if has_data(data_dir, t) else "FETCH"
        log.info("  %-6s %4d mentions  [%s]", t, c, status)

    log.info("%d to fetch, %d already have data, %d skipped", len(to_fetch), len(already), len(SKIP_TICKERS))

    if args.dry_run or not to_fetch:
        return

    ib = IB()
    ib.connect("127.0.0.1", args.port, clientId=args.client_id, timeout=15)
    log.info("Connected port %d", args.port)

    for i, (ticker, mention_count) in enumerate(to_fetch):
        log.info("── %s (%d/%d, %d mentions) ──", ticker, i + 1, len(to_fetch), mention_count)
        rows = fetch_ticker(ib, ticker, start, end, data_dir)
        log.info("  %s: %d rows", ticker, rows)

    ib.disconnect()
    log.info("Done. Fetched %d tickers.", len(to_fetch))


if __name__ == "__main__":
    main()
