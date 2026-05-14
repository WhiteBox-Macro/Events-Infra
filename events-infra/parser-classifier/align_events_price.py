#!/usr/bin/env python3
"""Align classified events with minute-bar price data.

For each event in events.classified, finds the corresponding minute bar
in SPY/QQQ parquet data and extracts a window of bars around it.

Output: events_aligned table or CSV with event metadata + price reactions
at t=0 (event bar), t+1, t+5, t+15, t+30, t+60 minutes.

Usage:
    python align_events_price.py --ticker SPY --window 60
    python align_events_price.py --ticker SPY --ticker QQQ --output csv
    python align_events_price.py --ticker SPY --output db
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dbkit import pg  # noqa: E402
from dbkit.constants import DB_BASE, load_dotenv_files  # noqa: E402

log = logging.getLogger("align")

PARQUET_DIR = Path(REPO_ROOT) / "events-infra" / "market-data" / "1m-parquet"
REACTION_OFFSETS = [0, 1, 2, 3, 5, 10, 15, 30, 60]


def load_price_data(ticker: str) -> pd.DataFrame:
    ticker_dir = PARQUET_DIR / ticker
    if not ticker_dir.exists():
        raise FileNotFoundError(f"No parquet dir for {ticker}: {ticker_dir}")

    frames = []
    for f in sorted(ticker_dir.glob("*.parquet")):
        frames.append(pd.read_parquet(f))

    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["ts_floor"] = df["timestamp"].dt.floor("min")
    log.info("loaded %s: %d bars, %s to %s", ticker, len(df),
             df["timestamp"].iloc[0].strftime("%Y-%m-%d"),
             df["timestamp"].iloc[-1].strftime("%Y-%m-%d"))
    return df


def fetch_classified_events() -> list[dict]:
    return pg.execute(
        "SELECT event_id, raw_id, publish_time, headline, text_content, "
        "is_regular, event_type, inferred_tone, inferred_magnitude, "
        "tickers, primary_ticker, sectors, primary_sector, countries, "
        "indicator_name, consensus_value, actual_value, surprise, "
        "classification_confidence "
        "FROM events.classified "
        "ORDER BY publish_time ASC"
    )


def find_event_bar(event_time: datetime, price_df: pd.DataFrame) -> int | None:
    """Find the index of the minute bar at or just after the event.

    If event falls outside trading hours, snaps forward to the next
    available bar. Returns None if no bar found within 24h.
    """
    event_floor = pd.Timestamp(event_time).floor("min").tz_localize("UTC") if event_time.tzinfo is None \
        else pd.Timestamp(event_time).floor("min")

    idx = price_df["ts_floor"].searchsorted(event_floor, side="left")

    if idx < len(price_df) and price_df.iloc[idx]["ts_floor"] == event_floor:
        return idx

    if idx < len(price_df):
        next_bar = price_df.iloc[idx]["ts_floor"]
        gap = (next_bar - event_floor).total_seconds()
        if gap <= 86400:
            return idx

    return None


def extract_reaction(event_idx: int, price_df: pd.DataFrame,
                     offsets: list[int]) -> dict:
    """Extract price at t+offset bars relative to event bar.

    LOOKAHEAD-SAFE CONVENTION:
      - open_t0  = PRE-EVENT price (bar opened before event fired)
      - close_t0 = FIRST POST-EVENT observation (bar closed after event)
      - ret_t0   = within-bar reaction = (close_t0 - open_t0) / open_t0
      - ret_tN   = (close_tN - open_t0) / open_t0 — all returns vs pre-event base

    This avoids lookahead: open_t0 is known before the event, close_t0
    is the first price observation AFTER the event. A backtest would
    enter at open_t1 (next bar open) at the earliest.
    """
    result = {}
    base_row = price_df.iloc[event_idx]
    result["bar_time_t0"] = base_row["timestamp"]
    result["open_t0"] = base_row["open"]
    result["close_t0"] = base_row["close"]
    result["volume_t0"] = base_row["volume"]

    pre_event_price = base_row["open"]

    # Within-bar reaction (event bar)
    result["ret_t0"] = (base_row["close"] - pre_event_price) / pre_event_price if pre_event_price else None

    # t-1 bar (context — what happened just before)
    if event_idx > 0:
        pre = price_df.iloc[event_idx - 1]
        result["close_t_minus1"] = pre["close"]
        result["bar_time_t_minus1"] = pre["timestamp"]
        result["ret_t_minus1"] = (pre["close"] - pre["open"]) / pre["open"] if pre["open"] else None
    else:
        result["close_t_minus1"] = None
        result["bar_time_t_minus1"] = None
        result["ret_t_minus1"] = None

    for offset in offsets:
        if offset == 0:
            continue
        target_idx = event_idx + offset
        if 0 <= target_idx < len(price_df):
            row = price_df.iloc[target_idx]
            result[f"close_t{offset}"] = row["close"]
            result[f"bar_time_t{offset}"] = row["timestamp"]
            result[f"ret_t{offset}"] = (row["close"] - pre_event_price) / pre_event_price if pre_event_price else None
        else:
            result[f"close_t{offset}"] = None
            result[f"bar_time_t{offset}"] = None
            result[f"ret_t{offset}"] = None

    return result


def align_all(events: list[dict], price_df: pd.DataFrame, ticker: str,
              offsets: list[int]) -> pd.DataFrame:
    rows = []
    matched = 0
    missed = 0

    for event in events:
        pub_time = event["publish_time"]
        idx = find_event_bar(pub_time, price_df)

        if idx is None:
            missed += 1
            continue

        reaction = extract_reaction(idx, price_df, offsets)
        pub_ts = pd.Timestamp(pub_time)
        if pub_ts.tzinfo is None:
            pub_ts = pub_ts.tz_localize("UTC")
        gap_sec = (reaction["bar_time_t0"] - pub_ts).total_seconds()

        row = {
            "event_id": event["event_id"],
            "publish_time": pub_time,
            "headline": event["headline"],
            "event_type": event["event_type"],
            "is_regular": event["is_regular"],
            "inferred_tone": event["inferred_tone"],
            "inferred_magnitude": event["inferred_magnitude"],
            "primary_ticker": event["primary_ticker"],
            "primary_sector": event["primary_sector"],
            "indicator_name": event.get("indicator_name"),
            "surprise": event.get("surprise"),
            "ticker_price": ticker,
            "snap_gap_sec": gap_sec,
            **reaction,
        }
        rows.append(row)
        matched += 1

    log.info("aligned %d events to %s bars (%d missed — outside price range)",
             matched, ticker, missed)
    return pd.DataFrame(rows)


def run(args: argparse.Namespace) -> int:
    events = fetch_classified_events()
    log.info("fetched %d classified events", len(events))

    all_frames = []
    for ticker in args.ticker:
        price_df = load_price_data(ticker)
        aligned = align_all(events, price_df, ticker, REACTION_OFFSETS)
        all_frames.append(aligned)

    combined = pd.concat(all_frames, ignore_index=True) if all_frames else pd.DataFrame()

    if combined.empty:
        log.warning("no aligned events")
        return 1

    # Summary stats
    log.info("=== Alignment Summary ===")
    log.info("total aligned rows: %d", len(combined))
    log.info("snap gap (sec): mean=%.1f median=%.1f max=%.1f",
             combined["snap_gap_sec"].mean(),
             combined["snap_gap_sec"].median(),
             combined["snap_gap_sec"].max())

    in_market = combined[combined["snap_gap_sec"] <= 60]
    log.info("events within 1 min of a bar: %d (%.1f%%)",
             len(in_market), 100 * len(in_market) / len(combined))

    if args.output == "csv":
        out_path = REPO_ROOT / "events-infra" / "market-data" / "events_aligned.csv"
        combined.to_csv(out_path, index=False)
        log.info("wrote %s (%d rows)", out_path, len(combined))

    elif args.output == "parquet":
        out_path = REPO_ROOT / "events-infra" / "market-data" / "events_aligned.parquet"
        combined.to_parquet(out_path, index=False)
        log.info("wrote %s (%d rows)", out_path, len(combined))

    elif args.output == "print":
        print("\n=== Sample aligned events (first 20) ===")
        cols = ["publish_time", "headline", "event_type", "inferred_tone",
                "ticker_price", "snap_gap_sec", "open_t0", "close_t0",
                "ret_t0", "ret_t1", "ret_t5", "ret_t15", "ret_t30", "ret_t60"]
        print(combined[cols].head(20).to_string(index=False))

        print("\n=== Reaction by tone ===")
        for tone in ["bullish", "bearish", "neutral"]:
            sub = combined[combined["inferred_tone"] == tone]
            if len(sub) > 5:
                print(f"\n{tone} (n={len(sub)}):")
                for col in ["ret_t0", "ret_t1", "ret_t5", "ret_t15", "ret_t30", "ret_t60"]:
                    vals = sub[col].dropna()
                    if len(vals):
                        print(f"  {col}: mean={vals.mean()*10000:.1f}bps median={vals.median()*10000:.1f}bps")

        print("\n=== Reaction by magnitude ===")
        for mag in ["major", "moderate", "minor"]:
            sub = combined[combined["inferred_magnitude"] == mag]
            if len(sub) > 5:
                print(f"\n{mag} (n={len(sub)}):")
                for col in ["ret_t5", "ret_t15", "ret_t60"]:
                    vals = sub[col].dropna()
                    if len(vals):
                        print(f"  {col}: mean={vals.mean()*10000:.1f}bps std={vals.std()*10000:.1f}bps")

        print("\n=== Regular events (scheduled releases) ===")
        reg = combined[combined["is_regular"] == True]  # noqa: E712
        if len(reg):
            print(f"n={len(reg)}")
            for col in ["ret_t0", "ret_t1", "ret_t5", "ret_t15", "ret_t30", "ret_t60"]:
                vals = reg[col].dropna()
                if len(vals):
                    print(f"  {col}: mean={vals.mean()*10000:.1f}bps std={vals.std()*10000:.1f}bps")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Align events with price data")
    parser.add_argument("--ticker", action="append", default=[], help="Ticker(s) to align (repeat for multiple)")
    parser.add_argument("--output", choices=["csv", "parquet", "print"], default="print")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if not args.ticker:
        args.ticker = ["SPY"]

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    env = load_dotenv_files()
    for k, v in env.items():
        os.environ.setdefault(k, v)

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
