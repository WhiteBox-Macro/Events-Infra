# sonnet_event_strategy.py

**Purpose:** LLM-as-tagger event-driven trading strategy. The LLM classifies events into categories; a deterministic algorithm decides whether/how to trade based on an impact table of historical returns per (category, ticker) pair.

## Architecture

1. LLM classifies: headline -> (category, sub_category, affected_tickers)
2. Impact table stores: category x ticker -> running return statistics (tone-adjusted)
3. Algorithm decides: lookup stats -> threshold check -> trade/no-trade
4. LLM has NO opinion on direction, magnitude, or whether to trade

## Key Classes

- **`ImpactRecord`** — single observation dataclass (category, ticker, actual_return)
- **`_CatStats`** — running statistics (count, mean, hit_rate, std) for one (category, ticker) pair
- **`ImpactTable`** — collection of `_CatStats`, supports `record()`, `lookup()`, `summary()`. Returns are tone-adjusted (bearish events flip sign so positive mean = "tone reliably predicts direction")
- **`SonnetEventStrategy`** — main strategy class implementing on_bar/on_event/refit protocol

## Key Functions

- **`decide_trade(stats, tone)`** — pure deterministic decision: checks min observations, hit rate, avg return. Returns (side, confidence, reason). Supports both tone-reliable and contrarian trading
- **`on_bar(tick, ctx)`** — tracks last bar prices, processes pending observations (records actual returns after HOLDING_BARS=15 bars)
- **`on_event(tick, ctx)`** — classifies event (cache-first), looks up impact stats, decides trade, emits orders. Populates `last_decisions` for dashboard visibility
- **`refit(train_start, train_end, ctx)`** — blacklists categories with n>=10 and hit_rate<0.45
- **`record_exit(ticker, actual_return, exit_time)`** — called by runner on position exit, updates impact table

## Inputs/Outputs

- **Inputs:** BarTick, EventTick from sequencer; StrategyContext for positions/prices
- **Outputs:** list[Order] from on_event; side effects on ImpactTable
- **Cache:** reads/writes `events_classified_cache.json` (from preclassify.py)

## Dependencies

- `anthropic` SDK (for live classification, skipped in cache_only mode)
- `tick.py` (BarTick, EventTick, Order)
- `engine.py` (StrategyContext)

## Parameters

- HOLDING_BARS=15, POSITION_SIZE_PCT=0.05, MIN_OBS_TO_TRADE=3
- MIN_HIT_RATE=0.55, MIN_AVG_RETURN_BPS=2.0, MAX_CONCURRENT_POSITIONS=3
- LLM endpoint defaults to `http://192.168.1.10:9210` (Rin proxy)

## Gotchas

- `cache_only=True` mode skips live LLM calls entirely; uncached events produce no trades
- Tone adjustment means bearish events have their return signs flipped before stats accumulation
- Pending observations track bars_elapsed per-ticker, so multi-ticker events create independent observation streams
