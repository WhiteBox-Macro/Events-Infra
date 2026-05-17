# dashboard/

Live replay dashboard for the event backtest. Streams backtest playback over WebSocket to a browser-based UI.

## Files

| File | Role |
|------|------|
| `server.py` | Entry point: HTTP server for index.html + WebSocket server for replay commands |
| `replay_driver.py` | State machine that drives replay: play/pause/seek/speed, processes timeline groups, emits WS messages |
| `index.html` | Single-file frontend: candlestick chart (LightweightCharts), event timeline, decision pipeline, positions, trade log |

## How It Works

1. `server.py` starts HTTP (port 8766) + WS (port 8767)
2. Browser loads `index.html`, connects to WS
3. Server sends init message (tickers, range) + events preview
4. User clicks Play; `ReplayDriver.run()` processes timeline groups and sends bar/event/fill/exit/portfolio/decision messages
5. Frontend renders chart, events panel, decision cards, positions table

## Usage

```
python server.py --tickers SPY QQQ NVDA --start 2024-10-01 --end 2024-12-31
# Open http://localhost:8766
```

## Dependencies

- websockets library
- LightweightCharts v4 (CDN, loaded by index.html)
- Strategy runs in cache_only mode (no live LLM calls)
