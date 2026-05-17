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

- **`decide_trade(stats, tone, params=GLOBAL_DEFAULTS, surprise=None)`** — pure deterministic decision. Dispatches by `params.side_rule`:
  - `tone_reliable` — default; uses min_obs, min_hit_rate, min_avg_bps. Also picks up contrarian trades when stats say so.
  - `contrarian` — strict: only trades when stats meet contrarian criterion.
  - `surprise_direction` — trade the sign of `surprise` directly. **IGNORES** stats, min_obs, min_hit_rate, min_avg_bps, and tone entirely. Confidence is always 1.0. Intended for scheduled releases (economic_data).
  - `sector_spillover` — **loud-fails (returns None, logs WARNING)** until B4 wires it. Reserved in the CHECK constraint so the migration accepts agent-proposed rows, but execution refuses.
  Returns `(side, confidence, reason)`. Default-arg `params` preserves zero-arg backward-compat.
- **`ImpactTable.lookup_with_fallback(category, ticker, primary_sector=None)`** — B3 cold-start chain (specific → sector → BROAD → "surprise_default"). Returns `(stats_or_None, fallback_level)`. **Opt-in: no internal caller flips it this session.**
- **`on_bar(tick, ctx)`** — tracks last bar prices, processes pending observations (records actual returns after HOLDING_BARS=15 bars)
- **`on_event(tick, ctx)`** — classifies event (cache-first), looks up impact stats, **fetches per-(cat, ticker) GateParams via `self._params_for(...)`**, decides trade, emits orders. Honors retired status from `signals.gate_params`. Populates `last_decisions` for dashboard visibility.
- **`compute_tilts(tick, ctx)`** — rebalance-mode counterpart of `on_event`. Honors retired-status blacklist AND per-(cat,ticker) GateParams: `params.tilt_unit`, `params.side_rule`, `params.min_obs`, `params.min_hit_rate` are all routed through `portfolio_allocator.compute_tilt(..., params=..., surprise=...)`. Same dispatch rules as `decide_trade`.
- **`refit(train_start, train_end, ctx)`** — blacklists categories with n>=10 and hit_rate<0.45; also calls `self.gate_registry.reload()` so newly-promoted agent gates take effect.
- **`record_exit(ticker, actual_return, exit_time)`** — called by runner on position exit, updates impact table

## Inputs/Outputs

- **Inputs:** BarTick, EventTick from sequencer; StrategyContext for positions/prices
- **Outputs:** list[Order] from on_event; side effects on ImpactTable
- **Cache:** reads/writes `events_classified_cache.json` (from preclassify.py)

## Dependencies

- `anthropic` SDK (for live classification, skipped in cache_only mode)
- `tick.py` (BarTick, EventTick, Order)
- `engine.py` (StrategyContext)
- `gate_params.py` (GateParams, GLOBAL_DEFAULTS, GateParamsRegistry, BROAD_TICKER)

## Parameters

- HOLDING_BARS=15, POSITION_SIZE_PCT=0.05, MAX_CONCURRENT_POSITIONS=3
- MIN_OBS_TO_TRADE / MIN_HIT_RATE / MIN_AVG_RETURN_BPS: legacy module aliases for `GLOBAL_DEFAULTS.{min_obs,min_hit_rate,min_avg_bps}` — kept for any callers that import them, but the canonical knob is the `GateParams` dataclass.
- LLM endpoint defaults to `http://192.168.1.10:9210` (Rin proxy)
- `gate_registry` (optional) — instance of `GateParamsRegistry`. If None, every `_params_for()` returns GLOBAL_DEFAULTS (pre-refactor behavior).

## Gotchas

- `cache_only=True` mode skips live LLM calls entirely; uncached events fall back to `event_type` as category with equal-weight tickers
- Tone adjustment means bearish events have their return signs flipped before stats accumulation
- Pending observations track bars_elapsed per-ticker, so multi-ticker events create independent observation streams
- `params.holding_bars` is read from GateParams but the **runner enforces HOLDING_BARS=15 globally** today. Per-(cat, ticker) holding_bars is wired through `decide_trade` but does not yet override the runner's scheduled-exit countdown — full plumbing is deferred to a follow-on session.
- `surprise_direction` rule **silently bypasses min_obs / min_hit_rate / min_avg_bps**. A user setting `min_obs=10` on a surprise_direction row will see the strategy trade on the first event with non-zero surprise. This is by design — surprise_direction is a stats-free shortcut — but worth knowing before populating `signals.gate_params`.
- Retired status check (`self._is_retired`) is O(1) against the in-memory `_retired` set; refreshes only at `refit()` time
- `ImpactTable.lookup_with_fallback` exists but no internal caller uses it this session (opt-in per implementation plan)
