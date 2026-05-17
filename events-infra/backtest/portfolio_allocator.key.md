# portfolio_allocator.py

Continuous weight-based portfolio rebalancing. Manages N tickers with target weights that shift via event-driven tilts and decay back to equal weight.

## Key Classes
- `AllocPosition` — single holding: ticker, qty (signed), avg_price, current_price
- `RebalOrder` — rebalancing order with target/current weight
- `PortfolioAllocator` — main class: positions, tilts, targets, cash, NAV

## Key Function
- `compute_tilt(category, ticker, tone, stats, llm_weight)` — standalone, returns weight delta
  - Formula: direction × magnitude_bps × 0.001 × llm_weight × hit_edge

## PortfolioAllocator Methods
- `initialize_positions(prices, ts)` — buy equal-weight portfolio
- `apply_event_tilts(tilts)` — add tilts, recompute + normalize targets
- `decay_tilts(dt_bars)` — exponential decay toward zero
- `mark_to_market(prices)` — update position prices
- `get_rebal_orders(prices)` — orders where |delta| > threshold
- `execute_rebal(orders, prices, ts)` — fill orders, update positions + cash
- `nav` property, `get_weights()`, `get_tilts()`, `get_target_weights()`
- `reset()` — clear all state for seek

## Parameters
MAX_WEIGHT=0.15, MIN_WEIGHT=-0.05, DECAY=0.997/bar, TILT_SCALE=0.001, REBAL_THRESHOLD=0.5%

## Gotchas
- Target weights are normalized to sum=1.0 after clamping
- Signed qty: positive=long, negative=short
- avg_price resets to entry price when position crosses zero
