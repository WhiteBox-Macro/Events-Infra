# index.html

**Purpose:** Single-file dashboard frontend for the event backtest replay. Connects to WebSocket server, renders candlestick charts, event timeline, decision pipeline, positions, and trade log.

## Layout (CSS Grid)

- **Top bar:** Play/Pause buttons, speed selectors (1m/s through MAX), progress bar, sim time display
- **Left panel:** Upcoming events (pre-loaded) + event history (most recent first, max 200)
- **Center:** Candlestick chart (LightweightCharts v4) with event/trade markers
- **Right panel:** Ticker selector with search + decision pipeline cards
- **Bottom:** Portfolio value, open positions table, trade log

## Key JS Objects/Functions

- **`S`** — global state: tickers, focused ticker, WebSocket ref, chart refs, bar buffers, markers, prices
- **`initMainChart()`** — creates LightweightCharts candlestick chart with dark theme, auto-resize
- **`buildTickerList()` / `focusTicker(ticker)`** — ticker selector UI, switches chart data
- **`connect()`** — WebSocket connection with auto-reconnect (2s delay)
- **Message handlers:** `onBar`, `onEvent`, `onFill`, `onExit`, `onPortfolio`, `onPlayback`, `onEventsPreview`, `onSeekDone`, `onDecision`, `onRefit`
- **`handle(m)`** — message router, handles `batch` type recursively

## Chart Updates

- Bar data buffered in `S.barBuf[ticker]` arrays
- Uses `requestAnimationFrame` coalescing to avoid redundant redraws
- Uses `setData()` (full replace) not `update()` for reliability
- Markers for events (arrows by tone) and trades (BUY/SELL arrows)

## Decision Pipeline Cards

Each card shows 4 steps: Event headline, Classification (category/tone/surprise), Impact Stats (n/avg_bps/hit_rate), Decision (reason + action). Color-coded borders: green=buy, red=sell, gray=skip. Refit cards shown in yellow.

## Dependencies

- LightweightCharts v4 (CDN)
- WebSocket server at `ws://localhost:__WS_PORT__` (port injected by server.py)

## Gotchas

- `__WS_PORT__` placeholder replaced server-side before serving
- Max 200 past events, 50 decision cards in DOM (older removed)
- Seek clears all buffers and markers
- No persistent state -- full reload on reconnect via init message
