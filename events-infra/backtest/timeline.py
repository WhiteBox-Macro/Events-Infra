"""Timeline merger — loads parquet bars + events.classified, merges into sorted tick stream."""
from __future__ import annotations

import heapq
import logging
import os
import sys
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path
from typing import Iterator

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tick import BarTick, EventTick, Tick  # noqa: E402

log = logging.getLogger("sequencer.timeline")

# Sort priority: bars before events at same timestamp
_PRIORITY_BAR = 0
_PRIORITY_EVENT = 1


def _load_parquet_bars(ticker: str, parquet_dir: Path) -> tuple[pd.DataFrame, np.ndarray]:
    ticker_dir = parquet_dir / ticker
    if not ticker_dir.exists():
        raise FileNotFoundError(f"No parquet dir: {ticker_dir}")

    frames = []
    for f in sorted(ticker_dir.glob("*.parquet")):
        frames.append(pd.read_parquet(f))

    df = pd.concat(frames, ignore_index=True).sort_values("timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    timestamps = df["timestamp"].values.astype("datetime64[ns]")
    log.info("loaded %s: %d bars (%s to %s)", ticker, len(df),
             df["timestamp"].iloc[0].strftime("%Y-%m-%d"),
             df["timestamp"].iloc[-1].strftime("%Y-%m-%d"))

    return df, timestamps


def _load_events() -> list[dict]:
    from dbkit import pg  # noqa: E402
    rows = pg.execute(
        "SELECT event_id, publish_time, event_type, is_regular, headline, "
        "inferred_tone, inferred_magnitude, tickers, primary_ticker, "
        "indicator_name, surprise, metadata "
        "FROM events.classified "
        "ORDER BY publish_time ASC"
    )
    log.info("loaded %d classified events", len(rows))
    return rows


def _bar_stream(ticker: str, df: pd.DataFrame):
    for idx, row in df.iterrows():
        ts = row["timestamp"].to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        yield (ts, _PRIORITY_BAR, ticker, idx, row)


def _event_stream(events: list[dict]):
    for i, ev in enumerate(events):
        ts = ev["publish_time"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        yield (ts, _PRIORITY_EVENT, "event", i, ev)


class TimelineMerger:
    def __init__(self, config):
        self.bar_dfs: dict[str, pd.DataFrame] = {}
        self.bar_timestamps: dict[str, np.ndarray] = {}

        parquet_dir = Path(REPO_ROOT) / config.parquet_dir
        for ticker in config.tickers:
            df, ts_arr = _load_parquet_bars(ticker, parquet_dir)

            if config.start_date:
                start = pd.Timestamp(config.start_date, tz="UTC")
                df = df[df["timestamp"] >= start].reset_index(drop=True)
            if config.end_date:
                end = pd.Timestamp(config.end_date, tz="UTC") + pd.Timedelta(days=1)
                df = df[df["timestamp"] < end].reset_index(drop=True)
                ts_arr = df["timestamp"].values.astype("datetime64[ns]")

            self.bar_dfs[ticker] = df
            self.bar_timestamps[ticker] = ts_arr
            log.info("filtered %s: %d bars", ticker, len(df))

        self.events = _load_events()
        if config.start_date:
            start_dt = datetime.strptime(config.start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            self.events = [e for e in self.events if e["publish_time"] >= start_dt]
        if config.end_date:
            end_dt = datetime.strptime(config.end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + \
                     __import__("datetime").timedelta(days=1)
            self.events = [e for e in self.events if e["publish_time"] < end_dt]
        log.info("filtered events: %d", len(self.events))

        self._total_ticks = sum(len(df) for df in self.bar_dfs.values()) + len(self.events)
        log.info("timeline: %d total ticks", self._total_ticks)

    @property
    def total_ticks(self) -> int:
        return self._total_ticks

    def _make_bar_tick(self, ticker: str, idx: int, row) -> BarTick:
        ts = row["timestamp"]
        if hasattr(ts, "to_pydatetime"):
            ts = ts.to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return BarTick(
            ticker=ticker, timestamp=ts,
            open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            volume=int(row["volume"]), bar_index=int(idx),
        )

    def _make_event_tick(self, ev: dict) -> EventTick:
        ts = ev["publish_time"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return EventTick(
            event_id=str(ev["event_id"]),
            publish_time=ts,
            event_type=ev["event_type"],
            is_regular=bool(ev["is_regular"]),
            headline=ev.get("headline"),
            inferred_tone=ev.get("inferred_tone", "neutral"),
            inferred_magnitude=ev.get("inferred_magnitude", "minor"),
            tickers=ev.get("tickers") or [],
            primary_ticker=ev.get("primary_ticker"),
            surprise=ev.get("surprise"),
            indicator_name=ev.get("indicator_name"),
            metadata=ev.get("metadata") or {},
        )

    def iter_grouped(self) -> Iterator[tuple[datetime, list[Tick]]]:
        """Yield (timestamp, [ticks]) groups in chronological order.

        Within each timestamp: bars first (sorted by ticker), then events.
        """
        streams = []
        for ticker in sorted(self.bar_dfs.keys()):
            streams.append(_bar_stream(ticker, self.bar_dfs[ticker]))
        streams.append(_event_stream(self.events))

        merged = heapq.merge(*streams, key=lambda x: (x[0], x[1], x[2]))

        for ts, group in groupby(merged, key=lambda x: x[0]):
            ticks = []
            for item in group:
                _, priority, source, idx, data = item
                if priority == _PRIORITY_BAR:
                    ticks.append(self._make_bar_tick(source, idx, data))
                else:
                    ticks.append(self._make_event_tick(data))
            yield ts, ticks
