#!/usr/bin/env python3
"""Sequencer runner — the main tick loop.

Modes:
  --verify-only    Timeline verification (no strategies)
  --run            Full backtest with strategies
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import BacktestConfig  # noqa: E402
from tick import BarTick, EventTick, Order, LookaheadViolation  # noqa: E402
from timeline import TimelineMerger  # noqa: E402
from engine import StrategyContext  # noqa: E402
from order_manager import OrderManager  # noqa: E402

log = logging.getLogger("sequencer.runner")

HOLDING_BARS = 15


def run_backtest(config: BacktestConfig, strategies: list) -> dict:
    """Full sequencer tick loop with strategies."""
    timeline = TimelineMerger(config)
    ctx = StrategyContext(timeline.bar_dfs, config.portfolio_notional)
    order_mgr = OrderManager(
        slippage_bps=config.slippage_bps,
        portfolio_notional=config.portfolio_notional,
    )

    stats = {"bar_ticks": 0, "event_ticks": 0, "orders_submitted": 0,
             "fills": 0, "exits": 0, "timestamps": 0, "refits": 0}
    last_prices: dict[str, float] = {}
    last_bar_per_ticker: dict[str, BarTick] = {}
    t0 = time.monotonic()

    wf = config.walk_forward
    wf_sim_start: datetime | None = None
    wf_last_refit: datetime | None = None
    wf_embargo_until: datetime | None = None

    for ts, ticks in timeline.iter_grouped():
        stats["timestamps"] += 1
        ctx.set_time(ts)

        if wf_sim_start is None:
            wf_sim_start = ts

        elapsed_days = (ts - wf_sim_start).total_seconds() / 86400
        if elapsed_days >= wf.initial_train_days:
            needs_refit = wf_last_refit is None or \
                (ts - wf_last_refit).total_seconds() / 86400 >= wf.refit_interval_days
            if needs_refit:
                for strat in strategies:
                    strat.refit(wf_sim_start, ts, ctx)
                wf_last_refit = ts
                wf_embargo_until = ts + timedelta(hours=wf.embargo_hours)
                stats["refits"] += 1

        in_embargo = wf_embargo_until and ts < wf_embargo_until

        bars = [t for t in ticks if isinstance(t, BarTick)]
        events = [t for t in ticks if isinstance(t, EventTick)]

        # 1. Process scheduled exits on these bars
        for bar in bars:
            exit_fills = order_mgr.process_bar(bar, ts)
            for fill in exit_fills:
                stats["exits"] += 1
                # Notify strategy of the exit for impact table update
                for strat in strategies:
                    pos_return = None
                    for cp in order_mgr.closed_positions:
                        if cp.ticker == fill.ticker and cp.exit_time == ts:
                            pos_return = cp.realized_pnl
                            break
                    if pos_return is not None and hasattr(strat, "record_exit"):
                        strat.record_exit(fill.ticker, pos_return, ts)

        # 2. MTM open positions
        for bar in bars:
            order_mgr.mark_positions(bar)
            last_prices[bar.ticker] = bar.close

        # 3. Update context cursors (strategies can now see this bar)
        for bar in bars:
            ctx.advance_cursor(bar.ticker, bar.bar_index, bar.close)
            last_bar_per_ticker[bar.ticker] = bar
            stats["bar_ticks"] += 1

        # Update context with current state
        ctx.set_positions(order_mgr.positions)
        portfolio_val = config.portfolio_notional
        for pos in order_mgr.positions.values():
            portfolio_val += pos.unrealized_pnl * pos.qty * pos.entry_price
        ctx.set_portfolio_value(portfolio_val)
        order_mgr.portfolio_value = portfolio_val

        # 4. Dispatch bars to strategies
        for bar in bars:
            for strat in strategies:
                orders = strat.on_bar(bar, ctx)

        # 5. Dispatch events to strategies
        for event in events:
            stats["event_ticks"] += 1
            for strat in strategies:
                orders = strat.on_event(event, ctx)
                if in_embargo:
                    orders = []
                for order in orders:
                    # Fill at most recent bar's close for this ticker
                    last_bar = last_bar_per_ticker.get(order.ticker)
                    if last_bar is None:
                        continue

                    fill = order_mgr.fill_at_close(order, last_bar, ts)
                    if fill:
                        stats["orders_submitted"] += 1
                        stats["fills"] += 1
                        order_mgr.schedule_exit(order.strategy, order.ticker, HOLDING_BARS)

        # Progress logging
        if stats["timestamps"] % 50000 == 0:
            elapsed = time.monotonic() - t0
            total = stats["bar_ticks"] + stats["event_ticks"]
            n_open = len(order_mgr.positions)
            n_closed = len(order_mgr.closed_positions)
            log.info("progress: %d ticks, %d fills, %d exits, %d open, %.0f ticks/s",
                     total, stats["fills"], stats["exits"], n_open, total / elapsed)

    # Close remaining positions
    order_mgr.close_all(last_prices, ts)
    elapsed = time.monotonic() - t0

    # Compute results
    total_pnl_pct = sum(p.realized_pnl for p in order_mgr.closed_positions if p.realized_pnl is not None)
    n_trades = len(order_mgr.closed_positions)
    wins = sum(1 for p in order_mgr.closed_positions if p.realized_pnl and p.realized_pnl > 0)
    losses = sum(1 for p in order_mgr.closed_positions if p.realized_pnl and p.realized_pnl < 0)
    avg_return = total_pnl_pct / n_trades if n_trades > 0 else 0

    results = {
        "elapsed_sec": round(elapsed, 2),
        "total_ticks": stats["bar_ticks"] + stats["event_ticks"],
        "total_trades": n_trades,
        "wins": wins,
        "losses": losses,
        "hit_rate": round(wins / n_trades * 100, 1) if n_trades > 0 else 0,
        "total_return_bps": round(total_pnl_pct * 10000, 2),
        "avg_return_bps": round(avg_return * 10000, 2),
        "fills": stats["fills"],
    }

    log.info("=== Backtest Complete ===")
    log.info("elapsed: %.1fs", elapsed)
    log.info("trades: %d (W:%d L:%d, hit=%.1f%%)", n_trades, wins, losses, results["hit_rate"])
    log.info("total return: %.1f bps, avg: %.1f bps/trade", results["total_return_bps"], results["avg_return_bps"])

    # Strategy-specific summaries
    for strat in strategies:
        if hasattr(strat, "impact"):
            log.info("--- Impact table (%s) ---", strat.name)
            for key, s in list(strat.impact.summary().items())[:15]:
                log.info("  %s: n=%d avg=%.1fbps hit=%.0f%%", key, s["n"], s["avg_bps"], s["hit%"])

    return results


def run_timeline_check(config: BacktestConfig) -> dict:
    """Phase 1: verify timeline plays back correctly."""
    timeline = TimelineMerger(config)
    stats = {"bar_ticks": 0, "event_ticks": 0, "timestamps": 0,
             "first_ts": None, "last_ts": None, "bars_per_ticker": {}}
    prev_ts = None
    t0 = time.monotonic()

    for ts, ticks in timeline.iter_grouped():
        stats["timestamps"] += 1
        if prev_ts is not None and ts < prev_ts:
            raise LookaheadViolation(f"timeline not sorted: {ts} < {prev_ts}")
        prev_ts = ts
        if stats["first_ts"] is None:
            stats["first_ts"] = ts
        stats["last_ts"] = ts

        events_seen = False
        for tick in ticks:
            if isinstance(tick, BarTick):
                stats["bar_ticks"] += 1
                stats["bars_per_ticker"][tick.ticker] = stats["bars_per_ticker"].get(tick.ticker, 0) + 1
                if events_seen:
                    raise LookaheadViolation(f"bar after event at {ts}")
            elif isinstance(tick, EventTick):
                stats["event_ticks"] += 1
                events_seen = True

        if stats["timestamps"] % 50000 == 0:
            elapsed = time.monotonic() - t0
            total = stats["bar_ticks"] + stats["event_ticks"]
            log.info("progress: %d timestamps, %d ticks, %.1f ticks/s",
                     stats["timestamps"], total, total / elapsed)

    elapsed = time.monotonic() - t0
    total = stats["bar_ticks"] + stats["event_ticks"]
    stats["elapsed_sec"] = round(elapsed, 2)
    stats["ticks_per_sec"] = round(total / elapsed, 1) if elapsed > 0 else 0

    log.info("=== Timeline Verification Complete ===")
    log.info("ticks: %d bars + %d events = %d total in %.1fs (%.0f/s)",
             stats["bar_ticks"], stats["event_ticks"], total, elapsed, stats["ticks_per_sec"])
    log.info("range: %s to %s", stats["first_ts"], stats["last_ts"])
    log.info("bars: %s", stats["bars_per_ticker"])
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Sequencer backtest runner")
    parser.add_argument("--tickers", nargs="+", default=["SPY", "QQQ"])
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--run", action="store_true", help="Full backtest with Sonnet strategy")
    parser.add_argument("--model", default="claude-sonnet-4-6", help="LLM model for event classification")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    from dbkit.constants import load_dotenv_files
    env = load_dotenv_files()
    for k, v in env.items():
        os.environ.setdefault(k, v)

    config = BacktestConfig(tickers=args.tickers, start_date=args.start, end_date=args.end)

    if args.verify_only:
        stats = run_timeline_check(config)
        print(f"\nVerification passed: {stats['bar_ticks'] + stats['event_ticks']} ticks in {stats['elapsed_sec']}s")
        return 0

    if args.run:
        from strategies.sonnet_event_strategy import SonnetEventStrategy
        strategy = SonnetEventStrategy(tickers=args.tickers, model=args.model)
        results = run_backtest(config, [strategy])
        print(f"\nResults: {json.dumps(results, indent=2)}")

        # Dump impact table
        if hasattr(strategy, "impact"):
            sm = strategy.impact.summary()
            print(f"\nImpact table ({len(sm)} categories):")
            for key, s in sorted(sm.items(), key=lambda x: -x[1]["n"]):
                print(f"  {key:30s}  n={s['n']:3d}  avg={s['avg_bps']:+6.1f}bps  hit={s['hit%']:4.1f}%")
        return 0

    log.info("use --verify-only or --run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
