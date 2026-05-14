#!/usr/bin/env python3
"""Pre-populate signals.price_cache with daily-close prices via yfinance.

The replay's HistoricalPriceSource reads this table to fill positions and
mark them to market; without a backfill it can't quote anything. Run this
once per experiment, covering the window + ticker universe you intend to
replay.

CLI:
    python -m trader.backtest.backfill_prices \\
        --tickers AAPL,MSFT,NVDA \\
        --from 2025-01-01 --to 2026-05-13 \\
        [--benchmarks SPY,QQQ]   # default: SPY

mark_at is set to 21:00 UTC on each trading day (≈ US close). The replay's
HistoricalPriceSource tolerance handles any rounding (it picks the nearest
mark within ±24h of the requested time).

Idempotent: PRIMARY KEY (ticker, price_at) on signals.price_cache means
re-runs upsert harmlessly.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, time as dtime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dbkit import pg  # noqa: E402
from dbkit.constants import load_dotenv_files  # noqa: E402

LOG = logging.getLogger("backfill_prices")

# US close is 16:00 ET; in UTC that's 20:00 (DST) or 21:00 (standard). We
# pick 21:00 UTC unconditionally — the HistoricalPriceSource tolerance
# bridges the hour gap during DST without an extra calendar dependency.
CLOSE_TIME_UTC = dtime(21, 0)
SOURCE_TAG = "yfinance_daily"


def _parse_date(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _yfinance():
    try:
        import yfinance  # type: ignore[import-untyped]
        return yfinance
    except ImportError as exc:
        raise RuntimeError(
            "yfinance not installed. Run `pip install -e \".[trader]\"`."
        ) from exc


def _download(tickers: Iterable[str], start: datetime, end: datetime):
    """Fetch daily bars for `tickers` across [start, end]. Returns a pandas
    DataFrame indexed by date with a column-MultiIndex of (field, ticker)."""
    yf = _yfinance()
    tickers = sorted({t.upper() for t in tickers if t})
    if not tickers:
        return None
    LOG.info("downloading daily bars for %d ticker(s) across %s → %s",
             len(tickers), start.date(), end.date())
    return yf.download(
        tickers=tickers,
        start=start.date().isoformat(),
        end=(end.date()).isoformat(),
        interval="1d",
        auto_adjust=True,
        progress=False,
        group_by="ticker" if len(tickers) > 1 else None,
        threads=True,
    )


def _iter_close_rows(df, ticker: str):
    """Yield (date, close) for one ticker from the multi-index frame."""
    # Single-ticker df is a plain DataFrame; multi-ticker is a column MultiIndex.
    try:
        sub = df[ticker]
    except (KeyError, TypeError):
        sub = df
    if "Close" not in sub.columns:
        return
    for ts, value in sub["Close"].dropna().items():
        # yfinance returns a tz-naive DatetimeIndex; treat as UTC midnight.
        dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else datetime.combine(ts, dtime.min)
        date_only = dt.date()
        mark_at = datetime.combine(date_only, CLOSE_TIME_UTC).replace(tzinfo=timezone.utc)
        yield mark_at, Decimal(str(float(value)))


def backfill(tickers: list[str], *, start: datetime, end: datetime) -> int:
    df = _download(tickers, start, end)
    if df is None or df.empty:
        LOG.warning("no data returned for %s", tickers)
        return 0

    inserted = 0
    for ticker in sorted({t.upper() for t in tickers}):
        rows = []
        for mark_at, price in _iter_close_rows(df, ticker):
            rows.append({
                "ticker": ticker,
                "price_at": mark_at,
                "price": price,
                "source": SOURCE_TAG,
                "cached_at": datetime.now(timezone.utc),
            })
        if not rows:
            LOG.info("%s: no rows", ticker)
            continue
        # Upsert one at a time so we don't bail on a single bad row.
        for r in rows:
            try:
                pg.upsert("signals.price_cache", r, conflict_on=["ticker", "price_at"])
                inserted += 1
            except Exception:
                LOG.exception("upsert failed for %s @ %s", ticker, r["price_at"])
        LOG.info("%s: cached %d rows", ticker, len(rows))
    return inserted


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill signals.price_cache with daily yfinance closes")
    ap.add_argument("--tickers", required=True, help="comma-separated tickers (e.g. AAPL,MSFT)")
    ap.add_argument("--from", dest="from_date", required=True, help="ISO date inclusive")
    ap.add_argument("--to", dest="to_date", required=True, help="ISO date exclusive")
    ap.add_argument("--benchmarks", default="SPY",
                    help="comma-separated benchmark tickers to also fetch (default SPY)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    load_dotenv_files()
    if not pg.get_pool():  # type: ignore[truthy-function]
        # get_pool() raises if DATABASE_URL is missing; this branch never executes.
        return 2

    start = _parse_date(args.from_date)
    end = _parse_date(args.to_date)
    if start >= end:
        LOG.error("--from must precede --to")
        return 2

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    benchmarks = [t.strip().upper() for t in (args.benchmarks or "").split(",") if t.strip()]
    universe = sorted(set(tickers) | set(benchmarks))

    n = backfill(universe, start=start, end=end)
    LOG.info("backfill complete: %d rows cached across %d ticker(s)", n, len(universe))
    return 0


if __name__ == "__main__":
    sys.exit(main())
