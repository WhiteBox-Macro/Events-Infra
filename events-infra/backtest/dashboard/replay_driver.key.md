# replay_driver.py

**Purpose:** State machine that drives backtest replay for the dashboard. Processes timeline groups one-by-one, respects play/pause/seek/speed commands, and emits WebSocket messages.

## State Machine

```
PAUSED -> (play) -> PLAYING -> (pause) -> PAUSED
Any -> (seek) -> fast-forward -> PAUSED
PLAYING -> end of timeline -> FINISHED
```

All transitions via an asyncio command queue -- no shared mutable state between tasks.

## Key Class: ReplayDriver

### Constructor
- Loads timeline via `TimelineMerger(config)`
- Builds lightweight index: stores (timestamp, bar_indices, event_indices) instead of full tick objects to save memory
- Pre-builds `_all_events` list for events_preview messages

### Command Methods (enqueue via asyncio.Queue)
- `play()`, `pause()`, `set_speed(s)`, `seek_pct(pct)`

### Core Methods
- **`run(send)`** — main async loop. Drains command queue, processes groups, manages timing/batching per speed setting
- **`_process_group(ts, bars, events)`** — full tick processing: refit check, exit fills, MTM, strategy dispatch, order fills, decision capture, portfolio snapshots
- **`_ff_group(ts, bars, events)`** — fast-forward (no messages, no sleep) for seek
- **`_maybe_refit(ts)`** — walk-forward refit with embargo
- **`_get_group(idx)`** — reconstructs (timestamp, bars, events) from lightweight index on-the-fly
- **`get_init_msg()`** — initial handshake message (tickers, range, counts)
- **`get_events_from_cursor(n)`** — upcoming events preview

## Speed Modes

- `1min` through `1day`: real-time scaling with gap-proportional delays
- `max`: batch mode, flush on important events or every 300s sim-time, 20ms real delay
- `>=3600` (1day): batch mode with 60s flush interval

## Message Types Emitted

bar, event, fill, exit, portfolio, decision, refit, playback, batch, seek_done, events_preview

## Dependencies

- config.py (BacktestConfig), tick.py, timeline.py (TimelineMerger)
- engine.py (StrategyContext), order_manager.py (OrderManager)

## Gotchas

- Seek replays entire history silently via `_ff_group` to rebuild state -- can be slow for large seeks
- Walk-forward embargo suppresses all orders during embargo window
- `_process_group` captures strategy `last_decisions` for decision pipeline UI
- Portfolio snapshot emitted on every trade or every 30th group
