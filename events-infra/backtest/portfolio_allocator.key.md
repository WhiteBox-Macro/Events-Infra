# portfolio_allocator.py

Continuous weight-based portfolio rebalancing. Manages N tickers with target weights that shift via event-driven tilts and decay back to equal weight.

## Key Classes
- `AllocPosition` — single holding: ticker, qty (signed), avg_price, current_price
- `RebalOrder` — rebalancing order with target/current weight
- `PortfolioAllocator` — main class: positions, tilts, targets, cash, NAV

## Key Function
- `compute_tilt(category, ticker, tone, stats, llm_weight, tilt_scale=TILT_SCALE, params=None, surprise=None)` — standalone, returns weight delta
  - Formula: `direction * llm_weight * tilt_unit` where `tilt_unit` is from `params.tilt_unit` (or module `TILT_UNIT=0.01` if params is None)
  - Dispatches by `params.side_rule` (when params given):
    - `tone_reliable` — legacy default; honors `params.min_obs` / `params.min_hit_rate`
    - `contrarian` — strict: only when stats meet contrarian criterion
    - `surprise_direction` — direction from `sign(surprise)`; ignores stats / tone
    - `sector_spillover` — loud-fails (logs WARNING, returns 0) until B4 wires it
  - `params=None` preserves pre-refactor behavior (effective: tone_reliable, min_obs=3, min_hit_rate=0.55)

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
