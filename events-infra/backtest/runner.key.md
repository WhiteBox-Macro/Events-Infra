# runner.py

**Purpose:** CLI entry point for running backtests. Two modes: `--verify-only` (timeline integrity check) and `--run` (full backtest with strategies).

## Key Functions

- **`run_backtest(config, strategies)`** — full tick loop: iterates TimelineMerger groups, dispatches bars/events to strategies, manages orders via OrderManager, handles walk-forward refit/embargo, closes all positions at end. Returns results dict with trade stats
- **`run_timeline_check(config)`** — verifies timeline is sorted, bars come before events at each timestamp, counts ticks per ticker. Raises LookaheadViolation on ordering errors
- **`main()`** — CLI with argparse: --tickers, --verify-only, --run, --model, --start, --end, -v

## Tick Loop Order (run_backtest)

1. Walk-forward refit check (expanding window, embargo)
2. Process scheduled exits on bars
3. MTM open positions
4. Update context cursors
5. Dispatch bars to strategies (on_bar)
6. Dispatch events to strategies (on_event), fill orders at bar close, schedule exits at HOLDING_BARS=15

## Inputs/Outputs

- **Input:** BacktestConfig, list of strategy instances
- **Output:** results dict: elapsed_sec, total_trades, wins, losses, hit_rate, total_return_bps, avg_return_bps
- **Side effect:** logs impact table summary for strategies that have one

## Dependencies

- config.py, tick.py, timeline.py, engine.py, order_manager.py
- strategies.sonnet_event_strategy (imported in --run mode)
- dbkit.constants (env loading)

## Gotchas

- HOLDING_BARS=15 is hardcoded here AND in the strategy -- must stay in sync
- Progress logging every 50,000 timestamps
- Force-closes all remaining positions at last known prices at backtest end
