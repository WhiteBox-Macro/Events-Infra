# tick.py

**Purpose:** Core data types for the backtest sequencer. Defines all tick, order, fill, and position dataclasses.

## Types

- **`BarTick`** (frozen, slots) — OHLCV bar: ticker, timestamp, open/high/low/close, volume, bar_index
- **`EventTick`** (frozen, slots) — classified event with the post-2026-05-18 unified shape: event_id, publish_time, event_category (14-label bucket), event_type (30-label fine), event_outcome (sub-classification, nullable), is_regular, headline, tone/magnitude/confidence (renamed from inferred_*), primary_ticker (objective truth, any ticker), ticker_impacts (list of `{ticker, weight, role}`, max 3, universe-only), sector (single nullable), indicator_name/consensus_value/actual_value/surprise/reporting_period (scheduled block), metadata.
- **`Tick`** — Union[BarTick, EventTick]
- **`Order`** (frozen, slots) — trade intent: strategy, ticker, side (buy/sell), qty_pct, reason, submitted_at, metadata
- **`Fill`** — executed order: adds qty, price, slippage_bps, fill_time
- **`Position`** — open/closed position: entry/exit prices and times, unrealized/realized PnL
- **`LookaheadViolation`** — exception for timeline ordering errors

## Gotchas

- BarTick and EventTick are frozen (immutable); Position and Fill are mutable
- Order.qty_pct is a fraction of portfolio, not absolute quantity
- All types are plain dataclasses with no behavior -- logic lives in engine/order_manager
