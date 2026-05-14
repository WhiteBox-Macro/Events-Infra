"""Mark-to-market tick.

For every open paper position:
  1. fetch a fresh quote via the price source (writes into signals.price_cache
     as a side-effect, which keeps backtests and the slow agent cheap),
  2. compute unrealized_pnl,
  3. insert a row into signals.mtm_history,
  4. prune mtm_history to the last MTM_HISTORY_PER_POSITION marks per position.

Also snapshots the watchlist's benchmark tickers (unique set across
signals.watchlist.benchmark_ticker) into signals.benchmark_marks so settle.py
can compute alpha without re-fetching at horizon time.

`run_once(clock, price_source)` is the entry point; the paper-jobs runner
calls it on its MTM cadence (default 60s).
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Optional

from dbkit import pg
from trader.clock import Clock, LiveClock
from trader.prices import PriceMissing, PriceSource, LivePriceSource

_log = logging.getLogger(__name__)

DEFAULT_HISTORY_PER_POSITION = 1440  # ≈ 24h of 1-minute marks


def _history_cap() -> int:
    try:
        return int(os.environ.get("MTM_HISTORY_PER_POSITION", DEFAULT_HISTORY_PER_POSITION))
    except (TypeError, ValueError):
        return DEFAULT_HISTORY_PER_POSITION


def _open_positions() -> list[dict]:
    return pg.execute(
        "SELECT position_id, ticker, side, qty, entry_price, mode "
        "FROM signals.paper_positions WHERE status = 'open'"
    )


def _unique_benchmark_tickers() -> list[str]:
    rows = pg.execute(
        "SELECT DISTINCT benchmark_ticker FROM signals.watchlist "
        "WHERE active = TRUE AND benchmark_ticker IS NOT NULL"
    )
    return [r["benchmark_ticker"].upper() for r in rows if r.get("benchmark_ticker")]


def _mark_position(pos: dict, price: Decimal, mark_at, conn) -> None:
    entry = Decimal(str(pos["entry_price"]))
    qty = Decimal(str(pos["qty"]))
    if pos["side"] == "long":
        unreal = qty * (price - entry)
    else:
        unreal = qty * (entry - price)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO signals.mtm_history "
            "(position_id, mark_at, mark_price, unrealized_pnl, mode) "
            "VALUES (%s, %s, %s, %s, %s)",
            [pos["position_id"], mark_at, price, unreal, pos["mode"]],
        )


def _mark_benchmark(ticker: str, price: Decimal, mark_at, mode: str, source_tag: str, conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO signals.benchmark_marks (ticker, mark_at, price, source, mode) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (ticker, mark_at, mode) DO NOTHING",
            [ticker, mark_at, price, source_tag, mode],
        )


def _prune_position(position_id: str, conn) -> None:
    cap = _history_cap()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM signals.mtm_history "
            "WHERE position_id = %s "
            "  AND id NOT IN ("
            "    SELECT id FROM signals.mtm_history "
            "    WHERE position_id = %s "
            "    ORDER BY mark_at DESC, id DESC "
            "    LIMIT %s"
            "  )",
            [position_id, position_id, cap],
        )


def run_once(*, clock: Optional[Clock] = None, price_source: Optional[PriceSource] = None) -> dict:
    """Single MTM pass. Returns a small status payload for logging."""
    clock = clock or LiveClock()
    price_source = price_source or LivePriceSource()
    now = clock.now()

    positions = _open_positions()
    marked = 0
    skipped = 0

    # Group price lookups by ticker so we don't fetch yfinance N times for N
    # positions on the same name.
    quotes: dict[str, Optional[Decimal]] = {}
    for pos in positions:
        ticker = pos["ticker"]
        if ticker not in quotes:
            try:
                quotes[ticker] = price_source.get_price(ticker, at=now)
            except PriceMissing:
                quotes[ticker] = None
            except Exception:
                _log.exception("mtm: price lookup failed for %s", ticker)
                quotes[ticker] = None

    # Benchmark prices for the universe in use.
    bench_quotes: dict[str, Optional[Decimal]] = {}
    for bench in _unique_benchmark_tickers():
        try:
            bench_quotes[bench] = price_source.get_price(bench, at=now)
        except PriceMissing:
            bench_quotes[bench] = None
        except Exception:
            _log.exception("mtm: benchmark lookup failed for %s", bench)
            bench_quotes[bench] = None

    source_tag = getattr(price_source, "source_tag", "yfinance")
    with pg.transaction() as conn:
        for pos in positions:
            price = quotes.get(pos["ticker"])
            if price is None:
                skipped += 1
                continue
            _mark_position(pos, price, now, conn)
            _prune_position(pos["position_id"], conn)
            marked += 1
        for bench, price in bench_quotes.items():
            if price is None:
                continue
            # benchmarks are tracked once per pass; the dispatcher's
            # backtest replay can read them by ticker + mark_at directly.
            mode = positions[0]["mode"] if positions else "live"
            _mark_benchmark(bench, price, now, mode, source_tag, conn)

    return {"marked": marked, "skipped": skipped, "benchmarks": sum(1 for v in bench_quotes.values() if v is not None)}
