#!/usr/bin/env python3
"""
Fetch 1-min OHLCV (including premarket) from IBKR via IB Gateway.
Uses N parallel workers with staggered connections.

For 1-min bars, IBKR pacing limits are LIFTED — only bars <= 30s are rate-limited.
Max 50 simultaneous open requests. Max duration per 1-min request: "1 D".

Usage:
    python fetch_ibkr.py --start 2024-10-01
    python fetch_ibkr.py --tickers SPY QQQ GLD USO --start 2024-10-01
    python fetch_ibkr.py --start 2024-10-01 --workers 4
    python fetch_ibkr.py --start 2024-10-01 --dry-run
"""

import argparse
import asyncio
import logging
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from ib_insync import IB, Stock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DEFAULT_TICKERS = ["SPY", "QQQ", "GLD", "USO"]
INTER_REQUEST_GAP = 0.5  # seconds between requests per worker (connection stability)
CONNECT_STAGGER = 3       # seconds between worker connections


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
                wait = 15 * (attempt + 1)
                log.warning("Pacing violation, waiting %ds (%d/3)", wait, attempt + 1)
                time.sleep(wait)
            elif "not connected" in str(e).lower() or "disconnect" in str(e).lower():
                log.warning("Disconnected (%d/3)", attempt + 1)
                return pd.DataFrame()  # caller will reconnect
            elif "timeout" in str(e).lower():
                log.warning("Timeout (%d/3), retrying after 5s", attempt + 1)
                time.sleep(5)
            else:
                log.warning("Fetch error: %s (%d/3)", e, attempt + 1)
                time.sleep(2)
    return pd.DataFrame()


def ensure_connected(ib: IB, port: int, client_id: int, ticker: str) -> bool:
    """Reconnect if disconnected. Returns True if connected."""
    if ib.isConnected():
        return True
    for attempt in range(5):
        try:
            log.info("%s: reconnecting (clientId %d, attempt %d/5)", ticker, client_id, attempt + 1)
            ib.disconnect()
            time.sleep(3)
            ib.connect("127.0.0.1", port, clientId=client_id, timeout=20)
            log.info("%s: reconnected", ticker)
            return True
        except Exception as e:
            log.warning("%s: reconnect failed: %s", ticker, e)
            time.sleep(5 * (attempt + 1))
    return False


def worker_fetch_ticker(ticker: str, start: datetime, end: datetime,
                        data_dir: Path, port: int, client_id: int,
                        stagger_delay: float) -> int:
    """One worker: stagger connect, fetch all days for one ticker, disconnect."""
    time.sleep(stagger_delay)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    ib = IB()
    try:
        ib.connect("127.0.0.1", port, clientId=client_id, timeout=20)
        log.info("%s: connected (clientId %d)", ticker, client_id)
    except Exception as e:
        log.error("%s: connect failed (clientId %d): %s", ticker, client_id, e)
        return 0

    contract = Stock(ticker, "SMART", "USD")
    try:
        if not ib.qualifyContracts(contract):
            log.warning("%s: not found on IBKR", ticker)
            ib.disconnect()
            return 0
    except Exception as e:
        log.warning("%s: qualify failed: %s", ticker, e)
        ib.disconnect()
        return 0

    buf: dict[tuple, list] = {}
    total_rows = 0
    cursor = start
    day_count = 0

    while cursor <= end:
        if cursor.weekday() >= 5:
            cursor += timedelta(days=1)
            continue

        if not ensure_connected(ib, port, client_id, ticker):
            log.error("%s: giving up after reconnect failures at %s", ticker, cursor.date())
            break

        df = fetch_day(ib, contract, cursor.replace(hour=23, minute=59, second=59))
        if not df.empty:
            key = (cursor.year, cursor.month)
            buf.setdefault(key, []).append(df)
            total_rows += len(df)

        day_count += 1

        # Flush completed months
        next_day = cursor + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        if next_day.month != cursor.month and (cursor.year, cursor.month) in buf:
            key = (cursor.year, cursor.month)
            merged = pd.concat(buf.pop(key))
            out = output_path(data_dir, ticker, key[0], key[1])
            n = save_month(out, merged)
            log.info("%s 1m %04d-%02d: %d rows", ticker, key[0], key[1], n)

        cursor += timedelta(days=1)
        time.sleep(INTER_REQUEST_GAP)

    # Flush remaining
    for key, frames in buf.items():
        merged = pd.concat(frames)
        out = output_path(data_dir, ticker, key[0], key[1])
        n = save_month(out, merged)
        log.info("%s 1m %04d-%02d: %d rows", ticker, key[0], key[1], n)

    ib.disconnect()
    log.info("%s: done (%d days, %d rows)", ticker, day_count, total_rows)
    return total_rows


def expand_work(tickers: list[str], start: datetime, end: datetime, data_dir: Path) -> list[tuple[str, datetime]]:
    """Return (ticker, effective_start) pairs, skipping fully completed tickers."""
    work = []
    for ticker in tickers:
        cursor = start
        effective_start = None
        while cursor <= end:
            if not has_month(data_dir, ticker, cursor.year, cursor.month):
                effective_start = cursor.replace(day=1)
                break
            if cursor.month == 12:
                cursor = cursor.replace(year=cursor.year + 1, month=1, day=1)
            else:
                cursor = cursor.replace(month=cursor.month + 1, day=1)
        if effective_start is None:
            log.info("%s: all months exist, skipping", ticker)
        else:
            work.append((ticker, effective_start))
    return work


def main():
    parser = argparse.ArgumentParser(description="Parallel IBKR 1-min data fetch")
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", default=None)
    parser.add_argument("--port", type=int, default=4001)
    parser.add_argument("--base-client-id", type=int, default=70)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc)
    data_dir = Path(args.data_dir) if args.data_dir else get_data_dir()

    work = expand_work(args.tickers, start, end, data_dir)
    if not work:
        log.info("Nothing to fetch.")
        return

    log.info("Tickers: %d to fetch, %d workers, stagger %ds, gap %.1fs",
             len(work), args.workers, CONNECT_STAGGER, INTER_REQUEST_GAP)
    log.info("Output: %s", data_dir)

    if args.dry_run:
        for ticker, eff_start in work:
            days = sum(1 for d in range((end - eff_start).days + 1) if (eff_start + timedelta(days=d)).weekday() < 5)
            log.info("  [DRY] %s: %s → %s (%d days)", ticker, eff_start.date(), end.date(), days)
        total_days = sum(sum(1 for d in range((end - s).days + 1) if (s + timedelta(days=d)).weekday() < 5) for _, s in work)
        est_min = total_days * INTER_REQUEST_GAP / 60 / args.workers
        log.info("  Estimated: %d requests, ~%.0f min with %d workers", total_days, est_min, args.workers)
        return

    grand_total = 0
    work_queue = list(work)

    # Process in batches of N workers. Each batch runs N tickers in parallel,
    # waits for all to finish, then starts the next batch.
    batch_num = 0
    while work_queue:
        batch = work_queue[:args.workers]
        work_queue = work_queue[args.workers:]
        batch_num += 1

        log.info("── Batch %d: %s ──", batch_num, [t for t, _ in batch])

        with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="w") as pool:
            futures = {}
            for i, (ticker, eff_start) in enumerate(batch):
                cid = args.base_client_id + i
                stagger = i * CONNECT_STAGGER
                f = pool.submit(worker_fetch_ticker, ticker, eff_start, end,
                                data_dir, args.port, cid, stagger)
                futures[f] = ticker

            for f in as_completed(futures):
                ticker = futures[f]
                try:
                    rows = f.result()
                    grand_total += rows
                    log.info("=== %s complete: %d rows ===", ticker, rows)
                except Exception as e:
                    log.error("=== %s failed: %s ===", ticker, e)

    log.info("Done. %d total rows across %d tickers.", grand_total, len(work))


if __name__ == "__main__":
    main()
