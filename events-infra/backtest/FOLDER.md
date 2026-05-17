# backtest/

Event-driven backtest engine for the AOTC-Signals events-infra pipeline. Replays 1-minute bars and classified events through strategies, manages orders/positions, and computes P&L.

## Architecture

The sequencer processes a merged timeline of bar ticks and event ticks in chronological order. Bars always come before events at the same timestamp (lookahead guard). Strategies receive ticks via `on_bar`/`on_event` and return Orders. The OrderManager fills orders and tracks positions with scheduled exits.

## Files

| File | Role |
|------|------|
| `tick.py` | Core data types: BarTick, EventTick, Order, Fill, Position |
| `engine.py` | StrategyEngine protocol + StrategyContext (lookahead-guarded state view) |
| `timeline.py` | TimelineMerger: loads parquet bars + DB events, merges into sorted stream |
| `order_manager.py` | Order fills, position tracking, scheduled exits, P&L |
| `config.py` | BacktestConfig, RiskLimits, WalkForwardConfig dataclasses |
| `runner.py` | CLI entry point: --verify-only (timeline check) or --run (full backtest) |
| `preclassify.py` | Batch LLM classification of events -> cache JSON |

## Subdirectories

| Dir | Role |
|-----|------|
| `strategies/` | Strategy implementations (currently: sonnet_event_strategy) |
| `dashboard/` | Live replay dashboard: HTTP+WS server + single-page frontend |

## Data Flow

1. `preclassify.py` pre-classifies events via LLM -> `events_classified_cache.json`
2. `runner.py --run` or `dashboard/server.py` loads timeline + strategy
3. TimelineMerger merges parquet bars + DB events
4. Sequencer loop: refit -> exits -> MTM -> bars -> events -> fills
5. Strategy uses cache for classification, impact table for trade decisions

## Walk-Forward

- Initial training: 90 days before first trade
- Refit interval: 30 days (expanding window)
- Embargo: 24 hours post-refit (no trading)
