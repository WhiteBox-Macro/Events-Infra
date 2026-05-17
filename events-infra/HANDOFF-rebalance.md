# Session Handoff: Portfolio Rebalancing Dashboard

## What This Session Built

Evolved the event-driven backtest dashboard from a discrete trade-on-event strategy to a **continuous portfolio rebalancing model** that always holds 15 tickers and tilts allocations based on event signals.

## Major Features Added

### 1. Decision Pipeline Visibility (Item 4 from prior session)
- `SonnetEventStrategy.last_decisions` populated per event evaluation
- New `decision` WebSocket message type with full reasoning chain
- One card per event grouping all affected tickers (no per-ticker duplication)
- Cards show: Event → Classification → Per-ticker impact stats + weights → Decision/reason
- Cache-only mode (no LLM during replay)

### 2. Walk-Forward Refit Controller (Item 6 from prior session)
- `_maybe_refit()` in replay_driver: 90-day initial warmup, 30-day refit cadence
- 24h embargo post-refit suppresses orders but keeps observations
- `SonnetEventStrategy.refit()` blacklists `(category, ticker)` with hit_rate < 45% after 10+ obs
- New `refit` WebSocket message, yellow-bordered frontend card
- Resets cleanly on seek to 0%

### 3. LLM Classification Schema Extension
- Added `sector_impact` (ordered sector list) and `ticker_impact_weights` (ticker → 0–1) to classification
- 219 Q4 2024 events re-classified via Rin proxy with new weighted schema
- Position sizing now proportional: `qty_pct = base × llm_weight`
- 28-ticker classification universe (all parquet equities)

### 4. Continuous Portfolio Rebalancing Mode (new)
- New file: `portfolio_allocator.py` — `PortfolioAllocator` class
- Initializes 15 tickers at equal weight (1/15 = 6.67%)
- `compute_tilt(category, ticker, tone, stats, llm_weight)` standalone function
  - Tilt = direction × llm_impact_weight × 1% (normalized to holding pct)
  - Direction from impact-table edge (tone_reliable / tone_contrarian)
- `apply_event_tilts()` → `_recompute_targets()` with normalization to sum=1
- `decay_tilts()` exponential decay toward base (0.997/bar, ~4hr half-life)
- `get_rebal_orders()` + `execute_rebal()` for incremental rebalancing
- Signed `qty` supports long/short positions
- `--mode rebalance|discrete` CLI flag; discrete mode preserved

### 5. Event-Only Rebalancing
- Portfolio only rebalances when events fire (not every bar)
- Tilts decay between events but no trades unless new signal arrives

### 6. Dashboard UI for Rebalance Mode
- Weight heatmap replaces position table: Ticker | Weight | Target | Tilt
- Color-coded over/underweight (green/red) deviations from base
- Rebalance log replaces trade log: shows ↑/↓ weight delta arrows
- Synthetic NAV: `real_portfolio_nav + sim_hours × $100 + N(0, $50)`

## Performance & Reliability Fixes

### Vectorized Timeline Index (25× speedup)
- `_build_index_fast()` replaces Python `heapq.merge` + `df.iterrows()` loop
- Uses numpy `lexsort` + `np.split` for chronological grouping
- 1.5M ticks indexed in 1.19s (vs ~30s hanging)

### HTTP DNS Lookup Bug (250× speedup)
- `DashboardHTTPHandler.address_string()` override skips reverse DNS
- Page load: 54s → 0.2s

### TradingView Chart Update
- Switched from `setData(buffer)` per bar to `update(bar)` + fallback `setData`
- `focusTicker` does `setData + fitContent` on ticker switch
- Eliminates viewport drift for sparse tickers (QQQ during market gaps)

### Adversarial Review Fixes
- Strategy state (impact table, blacklist, pending) resets on seek
- Embargo override copies dict instead of mutating aliased reference
- `_pending` list properly drops recorded entries (no memory leak)

## Configuration & Operational Notes

### Working CLI
```bash
python events-infra/backtest/dashboard/server.py \
  --mode rebalance \
  --tickers SPY QQQ GOOG MSFT AMZN TSLA DJT BA AMD META AAPL SMCI JPM TSM \
  --parquet-dir "C:\Users\wfl15\aotc-signals-data\events\market_data" \
  --start 2024-10-02 --end 2024-11-30 \
  --port 8770
```

### Data Gaps Identified (parquet ingestion incomplete)
- **NVDA**: Nov 2024 only 16/22 days, Dec 2024 only 10 days → excluded
- **QQQ**: Oct 2024 has only Oct 2 → start date shifted to Oct 2
- **META**: Dec 2024 only 1 day
- **SPY**: Mar 2025 only 1 day

### Key Parameters
```python
# portfolio_allocator.py
BASE_WEIGHT_DEFAULT = 1/15      # equal weight
MAX_WEIGHT = 0.15               # 15% cap
MIN_WEIGHT = -0.05              # 5% short allowed
DECAY_PER_BAR = 0.997           # half-life ~230 bars (4hr)
TILT_UNIT = 0.01                # 1% per unit of LLM impact_weight
REBAL_THRESHOLD = 0.001         # 0.1% min delta to trigger

# sonnet_event_strategy.py
HOLDING_BARS = 15
MIN_OBS_TO_TRADE = 3
MIN_HIT_RATE = 0.55
MIN_AVG_RETURN_BPS = 2.0
```

## Files Added/Modified

**New**:
- `backtest/portfolio_allocator.py` (+ .key.md)
- `backtest/FOLDER.md`, `dashboard/FOLDER.md`, `strategies/FOLDER.md`
- 10 `.key.md` files for scripts over 50 lines (per CLAUDE.md protocol)
- `HANDOFF-rebalance.md` (this file)

**Modified**:
- `backtest/dashboard/replay_driver.py` — rebalance mode branch, vectorized index, WF refit, decision emission, DNS fix
- `backtest/dashboard/server.py` — `--mode` flag, DNS skip in HTTP handler
- `backtest/dashboard/index.html` — `onDecision`, `onRefit`, `onRebal`, `onAllocation`, reset button, mode-aware UI
- `backtest/strategies/sonnet_event_strategy.py` — `compute_tilts()`, `last_decisions`, `refit()`, blacklist, cache_only
- `backtest/preclassify.py` — `--reclassify` flag, sector_impact + ticker_impact_weights schema
- `backtest/runner.py` — CLI walk-forward refit + embargo
- `backtest/events_classified_cache.json` — 219 events re-classified with new schema

## Next Steps

1. **Fix parquet ingestion gaps** — re-ingest NVDA Nov/Dec, QQQ Oct, META Dec, SPY Mar
2. **Pre-classify remaining 3,382 events** for full Oct 2024–May 2026 backtest
3. **Sector-weighted base allocation** (currently equal-weight, could use market cap or sector exposure targets)
4. **Multi-factor decay** — different decay rates per event category (Fed slower than earnings)
5. **Real backtest stats** — track actual returns vs synthetic NAV for performance validation
