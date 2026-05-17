# tick.py

**Purpose:** Core data types for the backtest sequencer. Defines all tick, order, fill, and position dataclasses.

## Types

- **`BarTick`** (frozen, slots) — OHLCV bar: ticker, timestamp, open/high/low/close, volume, bar_index
- **`EventTick`** (frozen, slots) — classified event: event_id, publish_time, event_type, is_regular, headline, inferred_tone/magnitude, tickers, primary_ticker, surprise, indicator_name, metadata
- **`Tick`** — Union[BarTick, EventTick]
- **`Order`** (frozen, slots) — trade intent: strategy, ticker, side (buy/sell), qty_pct, reason, submitted_at, metadata
- **`Fill`** — executed order: adds qty, price, slippage_bps, fill_time
- **`Position`** — open/closed position: entry/exit prices and times, unrealized/realized PnL
- **`LookaheadViolation`** — exception for timeline ordering errors

## Gotchas

- BarTick and EventTick are frozen (immutable); Position and Fill are mutable
- Order.qty_pct is a fraction of portfolio, not absolute quantity
- All types are plain dataclasses with no behavior -- logic lives in engine/order_manager
