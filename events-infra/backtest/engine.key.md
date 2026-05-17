# engine.py

**Purpose:** Strategy protocol definition and the StrategyContext that provides a read-only, lookahead-guarded view of backtest state to strategies.

## Key Types

- **`StrategyEngine`** (Protocol) — interface: `name`, `on_bar()`, `on_event()`, `refit()`
- **`StrategyContext`** — read-only state view with lookahead guards

## StrategyContext API

- `advance_cursor(ticker, bar_index, close)` — called by runner after MTM, before strategy dispatch
- `set_time(ts)`, `set_portfolio_value(value)`, `set_positions(positions)` — state setters (runner-only)
- `clock` — current simulation time
- `portfolio_value` — current portfolio value
- `price(ticker)` — last known close price (only fully elapsed bars)
- `bars(ticker, lookback_n)` — last N bars as list[dict], cannot see past cursor (lookahead guard)
- `positions(strategy_name)` — open positions for a given strategy

## Dependencies

- tick.py (BarTick, EventTick, Order, Position, LookaheadViolation)
- numpy (imported but used minimally)

## Gotchas

- Cursor starts at -1 per ticker; bars() returns empty list until first advance
- positions() filters by strategy name AND exit_time is None
- No enforcement that set_* methods are only called by the runner -- it's a convention
