"""Settle paper positions at horizon and write alpha back to signals.decisions.

A position is "due" when `clock.now() - entry_at >= decision.horizon_hours`.
For each due position:
  1. Close it via paper.execute.close_position (synthetic slippage applied).
  2. Look up the benchmark price at entry and at exit from
     signals.benchmark_marks (nearest-mark within a 60-min window).
  3. Compute raw_return = (exit_price / entry_price - 1)  [×-1 for shorts].
  4. Compute alpha_return = raw_return - benchmark_return.
  5. Update the decision row: raw_return, alpha_return, holding_hours,
     pending=false.

Edge cases:
  - Missing benchmark price: alpha is left NULL, but raw_return + pending=false
    still get written so the row no longer blocks reflect.py.
  - Position already closed (e.g. by supersede): we still settle the
    decision row using the recorded exit_price.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from dbkit import pg
from trader.clock import Clock, LiveClock
from trader.paper.execute import close_position
from trader.prices import PriceSource, LivePriceSource

_log = logging.getLogger(__name__)


def _due_positions(now: datetime) -> list[dict]:
    """Open positions whose decision horizon has elapsed."""
    return pg.execute(
        "SELECT pp.position_id, pp.decision_id, pp.ticker, pp.side, pp.qty, "
        "       pp.entry_price, pp.entry_at, pp.mode, "
        "       d.horizon_hours, d.tier, "
        "       w.benchmark_ticker "
        "FROM signals.paper_positions pp "
        "JOIN signals.decisions d ON d.decision_id = pp.decision_id "
        "LEFT JOIN signals.watchlist w ON w.ticker = pp.ticker "
        "WHERE pp.status = 'open' "
        "  AND (%s - pp.entry_at) >= make_interval(hours => d.horizon_hours)",
        [now],
    )


def _pending_decisions_for_closed_positions() -> list[dict]:
    """Positions closed by another worker (supersede.py) whose decision row
    is still pending=true. We retroactively settle them so reflect.py has a
    complete record."""
    return pg.execute(
        "SELECT pp.position_id, pp.decision_id, pp.ticker, pp.side, pp.qty, "
        "       pp.entry_price, pp.entry_at, pp.exit_price, pp.exit_at, pp.mode, "
        "       d.horizon_hours, d.tier, "
        "       w.benchmark_ticker "
        "FROM signals.paper_positions pp "
        "JOIN signals.decisions d ON d.decision_id = pp.decision_id "
        "LEFT JOIN signals.watchlist w ON w.ticker = pp.ticker "
        "WHERE pp.status = 'closed' "
        "  AND d.pending = TRUE "
        "  AND pp.exit_price IS NOT NULL"
    )


def _benchmark_price(ticker: str, at: datetime, mode: str, tolerance_min: int = 60) -> Optional[Decimal]:
    if not ticker:
        return None
    lo = at - timedelta(minutes=tolerance_min)
    hi = at + timedelta(minutes=tolerance_min)
    rows = pg.execute(
        "SELECT price, mark_at FROM signals.benchmark_marks "
        "WHERE ticker = %s AND mode = %s AND mark_at BETWEEN %s AND %s "
        "ORDER BY ABS(EXTRACT(EPOCH FROM (mark_at - %s))) ASC LIMIT 1",
        [ticker.upper(), mode, lo, hi, at],
    )
    if not rows:
        return None
    return Decimal(str(rows[0]["price"]))


def _write_outcome(*, decision_id: str, entry_price: Decimal, exit_price: Decimal,
                   side: str, entry_at: datetime, exit_at: datetime,
                   benchmark_ticker: Optional[str], mode: str) -> None:
    # Raw return per side. We store as a decimal fraction (0.0123 = +1.23%).
    if entry_price == 0:
        return
    if side == "long":
        raw = (exit_price - entry_price) / entry_price
    else:
        raw = (entry_price - exit_price) / entry_price

    alpha: Optional[Decimal] = None
    if benchmark_ticker:
        bench_entry = _benchmark_price(benchmark_ticker, entry_at, mode)
        bench_exit = _benchmark_price(benchmark_ticker, exit_at, mode)
        if bench_entry and bench_exit and bench_entry != 0:
            bench_ret = (bench_exit - bench_entry) / bench_entry
            alpha = raw - bench_ret

    holding_hours = Decimal(str((exit_at - entry_at).total_seconds() / 3600.0))
    pg.update(
        "signals.decisions",
        {
            "raw_return": raw,
            "alpha_return": alpha,
            "holding_hours": holding_hours,
            "pending": False,
        },
        {"decision_id": decision_id},
    )


def run_once(*, clock: Optional[Clock] = None, price_source: Optional[PriceSource] = None) -> dict:
    """Run one settle pass. Returns counts of closed + settled rows."""
    clock = clock or LiveClock()
    price_source = price_source or LivePriceSource()
    now = clock.now()

    closed = 0
    settled = 0

    # 1) Close due positions (paper_positions → closed) and settle their rows.
    for pos in _due_positions(now):
        try:
            realized = close_position(
                position_id=pos["position_id"],
                clock=clock,
                price_source=price_source,
                reason="horizon",
            )
            if realized is None:
                continue
            closed += 1
            # Re-read exit price/time from the closed row so we settle against
            # the same data that landed in paper_positions.
            row = pg.execute(
                "SELECT exit_price, exit_at FROM signals.paper_positions WHERE position_id = %s",
                [pos["position_id"]],
            )
            if not row or row[0].get("exit_price") is None:
                continue
            _write_outcome(
                decision_id=pos["decision_id"],
                entry_price=Decimal(str(pos["entry_price"])),
                exit_price=Decimal(str(row[0]["exit_price"])),
                side=pos["side"],
                entry_at=pos["entry_at"],
                exit_at=row[0]["exit_at"],
                benchmark_ticker=pos.get("benchmark_ticker"),
                mode=pos["mode"],
            )
            settled += 1
        except Exception:
            _log.exception("settle: failed to close+settle position %s", pos["position_id"])

    # 2) Pick up positions that were closed early (by supersede.py) whose
    # decision row is still pending — settle those now.
    for pos in _pending_decisions_for_closed_positions():
        try:
            _write_outcome(
                decision_id=pos["decision_id"],
                entry_price=Decimal(str(pos["entry_price"])),
                exit_price=Decimal(str(pos["exit_price"])),
                side=pos["side"],
                entry_at=pos["entry_at"],
                exit_at=pos["exit_at"],
                benchmark_ticker=pos.get("benchmark_ticker"),
                mode=pos["mode"],
            )
            settled += 1
        except Exception:
            _log.exception("settle: failed to settle pre-closed decision %s", pos["decision_id"])

    return {"closed": closed, "settled": settled}
