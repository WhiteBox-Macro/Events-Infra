# backtest/

Event-driven backtest engine for the AOTC-Signals events-infra pipeline. Replays 1-minute bars and classified events through strategies, manages orders/positions, and computes P&L.

## Architecture

The sequencer processes a merged timeline of bar ticks and event ticks in chronological order. Bars always come before events at the same timestamp (lookahead guard). Strategies receive ticks via `on_bar`/`on_event` and return Orders. The OrderManager fills orders and tracks positions with scheduled exits.

## Files

| File | Role |
|------|------|
| `tick.py` | Core data types: BarTick, EventTick (unified shape post-2026-05-18), Order, Fill, Position |
| `engine.py` | StrategyEngine protocol + StrategyContext (lookahead-guarded state view) |
| `timeline.py` | TimelineMerger: loads parquet bars + events.classified rows, merges into sorted stream |
| `order_manager.py` | Order fills, position tracking, scheduled exits, P&L |
| `portfolio_allocator.py` | Continuous N-ticker rebalancing (event-driven tilts with decay) |
| `gate_params.py` | GateParams dataclass + GateParamsRegistry (per-(cat, ticker) decision-gate tuning surface) |
| `config.py` | BacktestConfig, RiskLimits, WalkForwardConfig dataclasses |
| `runner.py` | CLI entry point: --verify-only (timeline check) or --run (full backtest) |

## Subdirectories

| Dir | Role |
|-----|------|
| `strategies/` | Strategy implementations (currently: sonnet_event_strategy) |
| `dashboard/` | Live replay dashboard: HTTP+WS server + single-page frontend |

## Data Flow (post-2026-05-18 unified classification refactor)

1. Upstream `parser-classifier/run_classify.py` runs the unified Sonnet prompt — single LLM call per event populates ALL columns of `events.classified` (event_category, event_type, event_outcome, tone, magnitude, primary_ticker, ticker_impacts JSONB, sector, ...).
2. `runner.py --run` or `dashboard/server.py` loads timeline + strategy + GateParamsRegistry.
3. TimelineMerger merges parquet bars + events.classified rows; `_make_event_tick` builds EventTick with the full unified shape.
4. Sequencer loop: refit -> exits -> MTM -> bars -> events -> fills.
5. Strategy reads structured fields directly off EventTick — NO cache, NO live LLM calls (single source of truth is PG).

Retired in this refactor: `preclassify.py` (Stage-2 batched Sonnet), `events_classified_cache.json` (JSON cache), `backfill_structural_tags.py` (cache → PG bridge). All deleted — single classifier pass eliminates the two-truth seam.

## Walk-Forward

- Initial training: 90 days before first trade
- Refit interval: 30 days (expanding window)
- Embargo: 24 hours post-refit (no trading)
