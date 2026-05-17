# order_manager.py

**Purpose:** Manages order fills, position tracking, scheduled exits, and P&L computation.

## Key Classes

- **`ScheduledExit`** — exit order scheduled for N bars in the future (strategy, ticker, bars_remaining, position_key)
- **`OrderManager`** — central position/order state

## OrderManager API

- **`fill_at_close(order, bar, current_time)`** — immediate fill at bar close with slippage. Creates Position, returns Fill. Sizes position as `qty_pct * portfolio_value / price`
- **`schedule_exit(strategy, ticker, bars_ahead)`** — schedule exit N bars from now
- **`process_bar(bar, current_time)`** — decrement bars_remaining on scheduled exits for this ticker; execute exits when countdown hits 0. Returns list of exit Fills
- **`mark_positions(bar)`** — update unrealized P&L for open positions matching this bar's ticker
- **`close_all(last_prices, current_time)`** — force-close all open positions (end-of-backtest)
- **`has_position(strategy, ticker)`** — check if position exists

## Position Key

Positions keyed by `(strategy, ticker)` tuple -- only one position per strategy per ticker at a time.

## Slippage

Applied symmetrically: buy pays `price * (1 + bps/10000)`, sell receives `price * (1 - bps/10000)`.

## Dependencies

- tick.py (BarTick, Order, Fill, Position, LookaheadViolation)

## Gotchas

- Only one position per (strategy, ticker) -- new fill overwrites existing
- Exit fills compute realized_pnl as fractional return, not dollar P&L
- `portfolio_value` is updated externally by the runner each tick
- Closed positions appended to `closed_positions` list (grows unbounded)
